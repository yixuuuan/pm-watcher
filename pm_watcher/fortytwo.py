"""
42 只读适配器（真实端点，已验真）。

行情：GET https://rest.ft.42.space/api/v1/market-data/stats?market=<逗号分隔的 0x 地址>
      → 200，无需鉴权。返回每个市场的 outcomeStats。

关键归一化（来自真实返回的验证）：
  42 的 outcome.price 是债券曲线上的代币价，不是概率。
  隐含概率 = outcome.marketCap / market.marketCap（各 outcome 的 marketCap 之和 == 市场 marketCap）。

42 用链上合约地址标识市场、且这个端点没有文本搜索，所以要先知道地址。
把"世界杯冠军"市场地址填进 KNOWN_MARKETS 或用环境变量 FORTYTWO_MARKETS 提供后即生效；
未配置时 search_markets 返回空（不再用假数据污染榜单）。
"""
from __future__ import annotations

import os
import httpx

from .model import Market, Outcome, PredictionClient

BASE = "https://rest.ft.42.space"

# 关键词 → 42 市场合约地址列表。匹配时按"键是查询的子串"，故把更具体的键放前面。
# 重要：42 确实有总冠军市场（0x38D8…，"2026 World Cup Winner ?"，49 个结果），
# 也有各小组"组内第一"市场。下面分别归类。
KNOWN_MARKETS: dict[str, list[str]] = {
    # 先放更具体的键，避免 "World Cup Group" 误匹配到 "world cup"
    "world cup group": [
        "0x79617da453fD28d5001489CEd8aE243233C5e227",  # Mexico, South Africa, Korea Republic, Czechia
        "0x9A1086161B0Fb18c0EA7BB5EA0D537bE1FD538C1",  # Canada, Bosnia & Herzegovina, Qatar, Switzerland
        "0xA819F347663C19E797AAD9D3BbFAc4245BEEc8D5",  # Brazil, Morocco, Haiti, Scotland
        "0xaCacc7e1B86F9653188AEdC251069f800C91f654",  # USA, Paraguay, Australia, Türkiye
        "0x6FbED1273a72B43725473143a07Bc14d04af2931",  # Netherlands, Japan, Sweden, Tunisia
        "0x2a6C69eFBa8Bbd97F178dc1d6eEe412F2b6A7f44",  # Spain, Cabo Verde, Saudi Arabia, Uruguay
        "0x5fCFEED93F062245D5696d5C2a8cC9de9c253f7D",  # Argentina, Algeria, Austria, Jordan
        "0x70A79B853b3544a3f7237521F3f4E021fA80ee57",  # Portugal, Congo DR, Uzbekistan, Colombia
        "0x3Bf771569E774D5CaCc05C4DEcA0Da7b133eEe62",  # England, Croatia, Ghana, Panama
    ],
    "world cup": [
        "0x38D8CA35d8662b2c6C94199497d787c93Aa34fEE",  # 2026 World Cup Winner ?（49 个结果，$305K 量）
    ],
}


class FortyTwoClient(PredictionClient):
    platform = "42"

    def __init__(self, base: str = BASE) -> None:
        # 42 的接口在挑剔的 WAF 后面：缺了浏览器常带的头会被 400 拒掉。
        # 补全 UA / Accept / Accept-Language / Origin / Referer 即可（纯只读，无需鉴权）。
        self._http = httpx.AsyncClient(base_url=base, timeout=20.0, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.42.space",
            "Referer": "https://www.42.space/",
        })
        self._markets = {k.lower(): v for k, v in KNOWN_MARKETS.items()}
        # 环境变量覆盖/补充： FORTYTWO_MARKETS="world cup=0xabc,0xdef; golden boot=0x123"
        env = os.getenv("FORTYTWO_MARKETS", "").strip()
        for part in env.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                self._markets[k.strip().lower()] = [a.strip() for a in v.split(",") if a.strip()]

    async def close(self) -> None:
        await self._http.aclose()

    async def search_markets(self, query: str, limit: int = 64) -> list[Market]:
        q = query.lower()
        addrs = next((v for k, v in self._markets.items() if k in q), None)
        if not addrs:
            return []  # 没配该类别的市场地址 → 空，不污染榜单
        r = await self._http.get("/api/v1/market-data/stats",
                                 params={"market": ",".join(addrs)})
        r.raise_for_status()
        out: list[Market] = []
        for m in r.json():
            out.append(self._parse(m))
            if len(out) >= limit:
                break
        return out

    async def search_matches(self, limit: int = 40) -> list[Market]:
        """42 的单场是【精确比分盘】（结果如 "CAN 0–1 BIH"）。
        从列表接口找出 'A vs B' 且双方为世界杯队的市场，再按比分把 marketCap
        份额汇总成 A 胜 / Draw / B 胜（比分 g1>g2 → A 胜，g1==g2 → 平，g1<g2 → B 胜）。
        无法解析的杂项结果（如 'Other'）不计入，三向和可能略小于 100%——这是
        诚实保留，不强行归一。"""
        import re
        from .polymarket import _split_vs
        from .names import is_wc_team
        r = await self._http.get("/api/v1/markets", params={
            "locale": "en", "limit": 300, "offset": 0,
            "order": "volume", "ascending": "false", "status": "live"})
        r.raise_for_status()
        rows = r.json()
        rows = rows.get("data", rows) if isinstance(rows, dict) else rows
        cand: list[tuple[str, str, tuple[str, str]]] = []
        for m in rows:
            ques = (m.get("question") or "").strip()
            sides = _split_vs(ques)
            if not sides or not (is_wc_team(sides[0]) and is_wc_team(sides[1])):
                continue
            addr = m.get("address") or m.get("market")
            if addr:
                cand.append((str(addr), ques, sides))
            if len(cand) >= limit:
                break
        if not cand:
            return []
        # 批量拉行情统计
        r2 = await self._http.get("/api/v1/market-data/stats",
                                  params={"market": ",".join(a for a, _, _ in cand)})
        r2.raise_for_status()
        stats = {str(s.get("market")): s for s in r2.json()}
        score = re.compile(r"^\s*\w{2,3}\s+(\d+)\s*[–—-]\s*(\d+)\s+\w{2,3}\s*$")
        out: list[Market] = []
        for addr, ques, (a, b) in cand:
            s = stats.get(addr)
            if not s:
                continue
            total = _f(s.get("marketCap")) or 0.0
            if total <= 0:
                continue
            agg = {a: 0.0, "Draw": 0.0, b: 0.0}
            for o in s.get("outcomeStats", []):
                mc = _f(o.get("marketCap")) or 0.0
                nm = str(o.get("name", ""))
                mt = score.match(nm)
                if mt:
                    g1, g2 = int(mt.group(1)), int(mt.group(2))
                    key = a if g1 > g2 else ("Draw" if g1 == g2 else b)
                elif nm.strip().lower() in ("draw", "tie"):
                    key = "Draw"
                else:
                    continue   # Other 等无法归类的比分，诚实丢弃
                agg[key] += mc / total
            outcomes = [Outcome(name=k, price=round(v, 4))
                        for k, v in agg.items() if v > 0]
            if len(outcomes) < 2:
                continue
            out.append(Market(platform="42", id=addr,
                              title=ques.replace(" vs. ", " vs "),
                              outcomes=outcomes,
                              volume=_f(s.get("totalVolume"))))
        return out

    @staticmethod
    def _parse(m: dict) -> Market:
        total = _f(m.get("marketCap")) or 0.0
        outcomes: list[Outcome] = []
        for o in m.get("outcomeStats", []):
            mc = _f(o.get("marketCap"))
            prob = (mc / total) if (mc is not None and total > 0) else None
            outcomes.append(Outcome(name=str(o.get("name", "")),
                                    price=round(prob, 4) if prob is not None else None))
        addr = str(m.get("market", ""))
        return Market(platform="42", id=addr,
                      title=f"42:{addr[:8]}…" if addr else "42 market",
                      outcomes=outcomes, volume=_f(m.get("totalVolume")))


class MockFortyTwoClient(PredictionClient):
    platform = "42"

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        from .model import matches
        data = [
            Market("42", "42-wc-winner", "世界杯冠军（2026）",
                   [Outcome("Spain", 0.20), Outcome("Argentina", 0.18),
                    Outcome("France", 0.14), Outcome("England", 0.11)]),
        ]
        return [m for m in data
                if matches(query, m.title, "world cup winner spain argentina")][:limit]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
