"""
Polymarket 只读适配器。

行情走 Gamma API：https://gamma-api.polymarket.com —— 公开、无需鉴权。
（下单才需要 CLOB API 的 HMAC 签名，本版不涉及。）

要点：
- /markets 返回市场列表，每个市场的 outcomes / outcomePrices 是【JSON 字符串】，要再 json.loads。
- 价格本身就是 0~1 的隐含概率。
- 没有稳定的全文搜索参数，这里取活跃市场后在客户端按关键词过滤；
  量大时可改用 /public-search?q= 或按 tag_id 过滤（见注释）。
"""
from __future__ import annotations

import json
import httpx

from .model import Market, Outcome, PredictionClient, iso_to_ts, matches

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"   # 历史价时间序列（回填用）


class PolymarketClient(PredictionClient):
    platform = "polymarket"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=GAMMA, timeout=20.0,
                                       headers={"User-Agent": "pm-watcher/0.1"})

    async def close(self) -> None:
        await self._http.aclose()

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        # 取按 24h 量排序的活跃未结束市场，再在本地按关键词过滤。
        # 备选：GET /public-search?q=<query> ；或先查 /sports、/tags 拿 tag_id 再 /markets?tag_id=
        r = await self._http.get("/markets", params={
            "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false", "limit": 500,
        })
        r.raise_for_status()
        rows = r.json()
        rows = rows.get("data", rows) if isinstance(rows, dict) else rows

        out: list[Market] = []
        for m in rows:
            title = m.get("question") or m.get("title") or ""
            if not matches(query, title, m.get("slug", "")):
                continue
            out.append(self._parse(m))
            if len(out) >= limit:
                break
        return out

    async def search_matches(self, limit: int = 40) -> list[Market]:
        """单场比赛盘：扫活跃事件，标题形如 'A vs B' 且双方都是世界杯 48 强。
        兼容两种形态：事件下单个三向市场（outcomes=[A,Draw,B]），
        或每个结果一个二元子市场（groupItemTitle=A/Draw/B，取 Yes 价）。"""
        from .names import is_wc_team
        # 分页扫描活跃事件——之前只取前 300 个(按量排序)，冷门小场排不进就被漏；
        # 这里翻多页，确保低成交量的世界杯对阵也能进来。
        rows = await self._scan_events({
            "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        })

        out: list[Market] = []
        for ev in rows:
            title = (ev.get("title") or "").strip()
            sides = _split_vs(title)
            if not sides or not (is_wc_team(sides[0]) and is_wc_team(sides[1])):
                continue
            mks = ev.get("markets") or []
            outcomes: list[Outcome] = []
            close = None
            vol = _to_float(ev.get("volume"))
            if len(mks) == 1:
                pm = self._parse(mks[0])
                if len(pm.outcomes) >= 2 and not _is_yesno(pm.outcomes):
                    from .names import canonical_country as _cc
                    _sc = {_cc(sides[0]), _cc(sides[1])}
                    outcomes = [Outcome(name=(o.name if _cc(o.name) in _sc else "Draw"), price=o.price) for o in pm.outcomes]
                close = pm.close_ts
            else:
                from .names import canonical_country as _cc
                _sc = {_cc(sides[0]), _cc(sides[1])}
                for sub in mks:
                    pm = self._parse(sub)
                    label = (pm.group or pm.title or "").strip()
                    y = pm.outcome("yes")
                    if label and y and y.price is not None:
                        nm = label if _cc(label) in _sc else "Draw"
                        outcomes.append(Outcome(name=nm, price=y.price))
                    close = close or pm.close_ts
            if len(outcomes) < 2:
                continue
            slug = ev.get("slug", "")
            out.append(Market(
                platform="polymarket", id=str(ev.get("id") or slug),
                title=title, outcomes=outcomes,
                url=f"https://polymarket.com/event/{slug}" if slug else None,
                close_ts=close or iso_to_ts(ev.get("endDate")), volume=vol))
            if len(out) >= limit:
                break
        return out

    async def _scan_events(self, params: dict, pages: int = 12, page: int = 100) -> list[dict]:
        """翻页扫 /events（Gamma 每页上限 100）。去重 + 无新增即停，绕开“按量取前 N 个漏掉冷门场”。"""
        out: list[dict] = []
        seen: set = set()
        for i in range(pages):
            p = dict(params, limit=page, offset=i * page)
            r = await self._http.get("/events", params=p)
            r.raise_for_status()
            rows = r.json()
            rows = rows.get("data", rows) if isinstance(rows, dict) else rows
            if not rows:
                break
            new = 0
            for ev in rows:
                eid = ev.get("id") or ev.get("slug")
                if eid in seen:
                    continue
                seen.add(eid)
                out.append(ev)
                new += 1
            if len(rows) < page or new == 0:
                break
        return out

    async def _wc_closed_events(self) -> tuple[list[dict], str | None]:
        """尽量取全【已关闭】的世界杯对阵事件：先深翻分页、从命中事件挖出 World Cup tag，
        再按 tag 拉全(已关闭的低量盘按量排序埋得很深，靠 tag 才取得全)。返回 (events, tag_id)。"""
        from .names import canonical_country, is_wc_team
        base = await self._scan_events({"closed": "true", "order": "volume24hr",
                                        "ascending": "false"}, pages=20)
        tag_id = None
        for ev in base:
            s = _split_vs((ev.get("title") or "").strip())
            if s and is_wc_team(canonical_country(s[0])) and is_wc_team(canonical_country(s[1])):
                for tg in (ev.get("tags") or []):
                    lab = ((tg.get("label") or "") + " " + (tg.get("slug") or "")).lower()
                    if "world" in lab and "cup" in lab:
                        tag_id = tg.get("id")
                        break
            if tag_id:
                break
        evs = list(base)
        if tag_id:
            evs += await self._scan_events({"closed": "true", "tag_id": str(tag_id)}, pages=40)
        return evs, tag_id

    async def wc_closed_index(self) -> tuple[dict, str | None]:
        """扫一次已关闭世界杯盘，建成 {frozenset({canonA,canonB}): event} 索引(主盘优先，跳过副盘)。
        回填时建一次、复用，避免每场重复全量扫描。返回 (index, tag_id)。"""
        from .names import canonical_country
        evs, tag_id = await self._wc_closed_events()
        idx: dict = {}
        for ev in evs:
            title = (ev.get("title") or "").strip()
            if "more markets" in title.lower():
                continue
            sides = _split_vs(title)
            if not sides:
                continue
            key = frozenset({canonical_country(sides[0]), canonical_country(sides[1])})
            idx.setdefault(key, ev)   # 首个(主盘)优先
        return idx, tag_id

    async def _closing_from_event(self, ev: dict, kickoff_ts: int) -> dict[str, tuple]:
        """从一个已关闭事件取每档开球前最后一笔历史价。返回 {规范 label: (price0_1, ts)}。"""
        from .names import canonical_country
        toks: list[tuple[str, str]] = []
        mks = ev.get("markets") or []
        sides = _split_vs(ev.get("title") or "")
        if len(mks) == 1:
            names = _loads_list(mks[0].get("outcomes"))
            ids = _loads_list(mks[0].get("clobTokenIds"))
            for nm, tid in zip(names, ids):
                toks.append((str(nm), tid))
        else:
            for sub in mks:
                label = (sub.get("groupItemTitle") or "").strip()
                names = _loads_list(sub.get("outcomes"))
                ids = _loads_list(sub.get("clobTokenIds"))
                for nm, tid in zip(names, ids):
                    if str(nm).strip().lower() == "yes" and label:
                        toks.append((label, tid))
        out: dict[str, tuple] = {}
        async with httpx.AsyncClient(base_url=CLOB, timeout=20.0,
                                     headers={"User-Agent": "pm-watcher/0.1"}) as clob:
            for label, tid in toks:
                try:
                    r = await clob.get("/prices-history", params={
                        "market": tid, "startTs": kickoff_ts - 7 * 86400,
                        "endTs": kickoff_ts, "fidelity": 60})
                    r.raise_for_status()
                    hist = r.json().get("history") or []
                    pre = [h for h in hist if h.get("t", 0) < kickoff_ts]
                    if pre:
                        cl = canonical_country(label)
                        if sides and cl not in {canonical_country(sides[0]), canonical_country(sides[1])}:
                            canon = "Draw"
                        else:
                            canon = "Draw" if str(label).strip().lower().startswith("draw") else cl
                        out[canon] = (float(pre[-1]["p"]), int(pre[-1]["t"]))
                except Exception:
                    pass
        return out

    async def closing_for_match(self, home: str, away: str, kickoff_ts: int) -> dict[str, tuple]:
        """单场便捷版(会全量扫一次)。批量回填请用 wc_closed_index + _closing_from_event。"""
        from .names import canonical_country
        idx, _ = await self.wc_closed_index()
        ev = idx.get(frozenset({canonical_country(home), canonical_country(away)}))
        return await self._closing_from_event(ev, kickoff_ts) if ev else {}


    @staticmethod
    def _parse(m: dict) -> Market:
        names = _loads_list(m.get("outcomes"))
        prices = _loads_list(m.get("outcomePrices"))
        outcomes = []
        for i, nm in enumerate(names):
            p = None
            if i < len(prices):
                try:
                    p = float(prices[i])
                except (TypeError, ValueError):
                    p = None
            outcomes.append(Outcome(name=str(nm), price=p))
        slug = m.get("slug", "")
        git = (m.get("groupItemTitle") or "").strip()
        return Market(
            platform="polymarket",
            id=str(m.get("conditionId") or m.get("id") or slug),
            title=m.get("question") or m.get("title") or "",
            outcomes=outcomes,
            url=f"https://polymarket.com/event/{slug}" if slug else None,
            close_ts=iso_to_ts(m.get("endDate")),
            volume=_to_float(m.get("volume")),
            group=git or None,
        )


class MockPolymarketClient(PredictionClient):
    platform = "polymarket"

    async def search_matches(self, limit: int = 40) -> list[Market]:
        import time
        now = time.time()
        return [
            Market("polymarket", "pm-mx-rsa", "Mexico vs South Africa",
                   [Outcome("Mexico", 0.62), Outcome("Draw", 0.22), Outcome("South Africa", 0.16)],
                   close_ts=now + 3600 * 20, volume=250000),
            Market("polymarket", "pm-nl-jp", "Netherlands vs Japan",
                   [Outcome("Netherlands", 0.48), Outcome("Draw", 0.27), Outcome("Japan", 0.25)],
                   close_ts=now + 3600 * 44, volume=120000),
        ][:limit]

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        # 仿真真实结构：世界杯冠军 = 每支球队一个二元市场，group 给队名
        teams = {"Spain": 0.1585, "Argentina": 0.16, "France": 0.15, "England": 0.12}
        data = [
            Market("polymarket", f"pm-wc-{t.lower()}",
                   f"Will {t} win the 2026 FIFA World Cup?",
                   [Outcome("Yes", p), Outcome("No", round(1 - p, 4))],
                   url="https://polymarket.com/event/2026-fifa-world-cup-winner-595",
                   group=t)
            for t, p in teams.items()
        ]
        return [m for m in data if matches(query, m.title)][:limit]


def _split_vs(title: str) -> tuple[str, str] | None:
    """'Mexico vs South Africa' / 'A vs. B' -> (A, B)；不形如对阵则 None。"""
    import re
    m = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(m) != 2:
        return None
    a = m[0].split(":")[-1].strip()       # 去掉可能的前缀 "World Cup: "
    b = re.split(r"[|(–-]", m[1])[0].strip()  # 去掉尾注 "(June 11)" 等
    return (a, b) if a and b else None


def _is_yesno(outcomes) -> bool:
    names = {o.name.strip().lower() for o in outcomes}
    return names <= {"yes", "no"}


def _loads_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
