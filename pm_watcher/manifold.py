"""
Manifold 只读适配器（公开 API、无需鉴权、play-money 预测者共识）。

冠军市场：GET https://api.manifold.markets/v0/market/<id>
  → MULTIPLE_CHOICE，answers:[{text:队名, probability:0~1}]，和≈100%。
  probability 已是概率，直接用。

注意：Manifold 是 play-money（Mana），不是现金盘，作为"预测者共识"信号看待。
"""
from __future__ import annotations

import httpx

from .model import Market, Outcome, PredictionClient

BASE = "https://api.manifold.markets/v0"

# 关键词 → Manifold 市场 id（按"键是查询子串"匹配，更具体的放前面）
KNOWN_MARKETS: dict[str, str] = {
    "world cup": "JRzL2QcArhM674YSO4d8",  # 2026 FIFA World Cup ⚽ | 🏆 Winner（50 队，$123K 量）
}


class ManifoldClient(PredictionClient):
    platform = "manifold"

    def __init__(self, base: str = BASE) -> None:
        self._http = httpx.AsyncClient(base_url=base, timeout=20.0,
                                       headers={"User-Agent": "pm-watcher/0.1"})

    async def close(self) -> None:
        await self._http.aclose()

    async def search_markets(self, query: str, limit: int = 64) -> list[Market]:
        q = query.lower()
        mid = next((v for k, v in KNOWN_MARKETS.items() if k in q), None)
        if not mid:
            return []
        r = await self._http.get(f"/market/{mid}")
        r.raise_for_status()
        m = r.json()
        outcomes: list[Outcome] = []
        for a in m.get("answers", []):
            if a.get("isOther"):
                continue
            p = a.get("probability")
            outcomes.append(Outcome(name=str(a.get("text", "")),
                                    price=float(p) if p is not None else None))
        return [Market(platform="manifold", id=mid,
                       title=m.get("question", "Manifold"),
                       outcomes=outcomes, url=m.get("url"),
                       volume=_f(m.get("volume")))]


    async def search_matches(self, limit: int = 40) -> list[Market]:
        """单场比赛盘：用公开搜索接 'world cup' 开放市场，留下 'A vs B' 且双方是 48 强的
        MULTIPLE_CHOICE（answers 即 A/Draw/B）。"""
        from .polymarket import _split_vs
        from .names import is_wc_team
        r = await self._http.get("/search-markets", params={
            "term": "world cup", "limit": 100, "filter": "open"})
        r.raise_for_status()
        out: list[Market] = []
        for m in r.json():
            title = (m.get("question") or "").strip()
            sides = _split_vs(title)
            if not sides or not (is_wc_team(sides[0]) and is_wc_team(sides[1])):
                continue
            if m.get("outcomeType") != "MULTIPLE_CHOICE":
                continue
            mid = m.get("id")
            d = await self._http.get(f"/market/{mid}")
            if d.status_code != 200:
                continue
            full = d.json()
            outcomes = [Outcome(name=str(a.get("text", "")),
                                price=float(a["probability"]))
                        for a in full.get("answers", [])
                        if a.get("probability") is not None and not a.get("isOther")]
            if len(outcomes) < 2:
                continue
            out.append(Market(platform="manifold", id=str(mid), title=title,
                              outcomes=outcomes, url=full.get("url"),
                              close_ts=(full.get("closeTime") or 0) / 1000 or None,
                              volume=_f(full.get("volume"))))
            if len(out) >= limit:
                break
        return out


class MockManifoldClient(PredictionClient):
    platform = "manifold"

    async def search_matches(self, limit: int = 40) -> list[Market]:
        import time
        now = time.time()
        return [Market("manifold", "mf-mx-rsa", "Mexico vs South Africa (World Cup)",
                       [Outcome("Mexico", 0.58), Outcome("Draw", 0.25), Outcome("South Africa", 0.17)],
                       close_ts=now + 3600 * 20, volume=4200)][:limit]

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        from .model import matches
        data = [Market("manifold", "mf-wc", "2026 World Cup Winner",
                       [Outcome("Spain", 0.175), Outcome("France", 0.170),
                        Outcome("England", 0.101), Outcome("Argentina", 0.088)])]
        return [m for m in data if matches(query, m.title, "world cup winner")][:limit]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
