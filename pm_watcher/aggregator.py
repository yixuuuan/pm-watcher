"""
聚合器：把同一个关键词 query 并发打到各家，归一化后并排比较。

注意：跨平台"同一个市场"的精确匹配本身是个难题（各家标题/ticker 写法不同）。
本版做的是【同一关键词 + 同一结果名】的粗匹配比价——足够先把循环跑通、肉眼对齐；
语义级的市场配对留到后续步骤再做。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .model import Market, PredictionClient


@dataclass
class Quote:
    platform: str
    market_title: str
    price: float | None
    url: str | None


async def fetch_all(clients: list[PredictionClient], query: str,
                    limit: int = 10) -> tuple[dict[str, list[Market]], dict[str, str]]:
    """返回 (各平台市场列表, 各平台错误信息)。出错的平台 markets 为空、errors 有值。"""
    async def one(c: PredictionClient):
        try:
            return c.platform, await c.search_markets(query, limit=limit), None
        except Exception as e:
            return c.platform, [], f"{type(e).__name__}: {e}"
    triples = await asyncio.gather(*(one(c) for c in clients))
    results = {p: r for p, r, _ in triples}
    errors = {p: e for p, _, e in triples if e}
    return results, errors


def compare_outcome(results: dict[str, list[Market]], outcome_name: str) -> list[Quote]:
    """
    在每个平台为 outcome_name 取一个价格，兼容两种市场形态：
      1) 多结果市场（Polymarket/42）：找名字含 outcome_name 的 outcome，用其价格。
      2) 每结果一个二元市场（Kalshi）：找标题含 outcome_name 的市场，用其 "Yes" 价。
    """
    s = outcome_name.lower()
    quotes: list[Quote] = []
    for platform, markets in results.items():
        best: Quote | None = None
        for m in markets:
            if not isinstance(m, Market):
                continue
            o = m.outcome(outcome_name)          # 形态 1
            if o and o.price is not None:
                best = Quote(platform, m.title, o.price, m.url)
                break
            if s in m.title.lower():             # 形态 2
                yes = m.outcome("yes")
                if yes and yes.price is not None:
                    best = Quote(platform, m.title, yes.price, m.url)
                    break
        quotes.append(best or Quote(platform, "—", None, None))
    return quotes


def spread(quotes: list[Quote]) -> float | None:
    """各平台对同一结果的最高价与最低价之差——价差越大，潜在套利/分歧越明显。"""
    ps = [q.price for q in quotes if q.price is not None]
    return round(max(ps) - min(ps), 4) if len(ps) >= 2 else None


# ---------------- 第 2 步：跨平台配对 ----------------
import re

_WIN_RE = re.compile(r"will\s+(.+?)\s+win\b", re.I)


def outcome_label(m: Market, o: Outcome) -> str | None:
    """
    把"某市场的某结果"归一成一个跨平台可比的标签（如队名 'Spain'）。
      - 二元 Yes 结果：优先用 market.group（Polymarket groupItemTitle），
        否则从标题 'Will X win ...' 提取；都没有则用标题。
      - 二元 No 结果：跳过（不进榜）。
      - 多结果市场：outcome 名本身就是标签（42 那种）。
    """
    name = o.name.strip().lower()
    if name == "no":
        return None
    if name == "yes":
        if m.group:
            return m.group.strip()
        mm = _WIN_RE.search(m.title)
        return mm.group(1).strip() if mm else m.title.strip()
    return o.name.strip()


async def fetch_matches(clients) -> dict[str, list[Market]]:
    """并发拉各平台'单场比赛'盘。"""
    import asyncio
    res = await asyncio.gather(*[c.search_matches() for c in clients],
                               return_exceptions=True)
    out: dict[str, list[Market]] = {}
    for c, r in zip(clients, res):
        out[c.platform] = r if isinstance(r, list) else []
    return out


def build_matchboard(results: dict[str, list[Market]]) -> list[dict]:
    """
    跨平台归并单场比赛：key = 排序后的(规范队A, 规范队B)。
    每行: {teams:[A,B], title, kickoff, vol, odds:{platform:{A:p, Draw:p, B:p}}}
    kickoff 用市场收盘时间近似（诚实标注：收盘≈开球，非官方赛程）。
    """
    from .names import canonical_country
    from .polymarket import _split_vs
    rows: dict[tuple, dict] = {}
    for platform, markets in results.items():
        for m in markets:
            sides = _split_vs(m.title or "")
            if not sides:
                continue
            a, b = canonical_country(sides[0]), canonical_country(sides[1])
            key = tuple(sorted([a, b]))
            row = rows.setdefault(key, {"teams": [a, b], "title": f"{a} vs {b}",
                                        "kickoff": None, "vol": 0.0, "odds": {}})
            od: dict[str, float] = {}
            for o in m.outcomes:
                nm = o.name.strip()
                if o.price is None:
                    continue
                label = "Draw" if nm.lower() in ("draw", "tie") else canonical_country(nm)
                if label in (a, b) or label == "Draw":
                    od[label] = round(o.price * 100, 1)
            if od:
                row["odds"][platform] = od
            if m.close_ts and (row["kickoff"] is None or m.close_ts < row["kickoff"]):
                row["kickoff"] = m.close_ts
            if m.volume:
                row["vol"] += m.volume
    out = [r for r in rows.values() if r["odds"]]
    out.sort(key=lambda r: (r["kickoff"] or 9e18))
    return out


def build_board(results: dict[str, list[Market]]) -> dict[str, dict[str, float]]:
    """
    汇总成排行榜：label（规范队名）-> {platform: price}。
    自动发现所有结果，并把各平台不同写法的同一队合并。
    """
    from .names import canonical_country
    board: dict[str, dict[str, float]] = {}
    for platform, markets in results.items():
        for m in markets:
            if not isinstance(m, Market):
                continue
            for o in m.outcomes:
                if o.price is None:
                    continue
                label = outcome_label(m, o)
                if not label:
                    continue
                board.setdefault(canonical_country(label), {}).setdefault(platform, o.price)
    return board


def _norm(label: str) -> str:
    """轻度归一，缓解大小写/空格差异导致的同名未对齐。"""
    return " ".join(label.split()).title()
