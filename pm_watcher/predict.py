"""
Predict.fun 只读适配器（前端公开 GraphQL，无需账号/Key）。

端点：POST https://graphql.predict.fun/graphql
（注意：它的官方 REST API 主网要 x-api-key；这里用的是网页前端的公开 GraphQL，
 introspection 开放、行情查询无鉴权，纯只读。）

实地验证（2026-06-12 浏览器侦察）：
- 冠军盘：category(id:"2026-fifa-world-cup-winner") → markets 每队一个二元市场，
  market.chancePercentage 即概率（0~100）。实测 Spain 18 / France 17 / Portugal 12。
- 赛程+单场盘：categories(filter:{tag:"113"})（tag 113 = World Cup）一次拉 ~100 个
  category，其中 80 个是 fifwc-* 单场（带 startsAt 真实开球时间）；每场的 markets
  是 [主队码, Draw, 客队码] 三个二元市场（如 KOR 38 / Draw 32 / CZE 32）。
- 结果名是三字码（KOR/CZE），按位置映射回 category 标题里的两队（A vs B）。
"""
from __future__ import annotations

import httpx

from .model import Market, Outcome, PredictionClient, iso_to_ts

GQL = "https://graphql.predict.fun/graphql"

WINNER_CATEGORY = "2026-fifa-world-cup-winner"
WC_TAG = "113"   # categoryTags 里 "World Cup" 的 id（实地核实）


class PredictClient(PredictionClient):
    platform = "predict"

    def __init__(self, url: str = GQL) -> None:
        self._url = url
        self._http = httpx.AsyncClient(timeout=20.0, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://predict.fun",
            "Referer": "https://predict.fun/",
        })

    async def close(self) -> None:
        await self._http.aclose()

    async def _gql(self, query: str) -> dict | None:
        r = await self._http.post(self._url, json={"query": query})
        r.raise_for_status()
        j = r.json()
        return None if j.get("errors") else j.get("data")

    async def search_markets(self, query: str, limit: int = 64) -> list[Market]:
        if "world cup" not in query.lower():
            return []
        data = await self._gql(
            '{category(id:"%s"){title markets{edges{node{'
            'title chancePercentage status}}}}}' % WINNER_CATEGORY)
        cat = (data or {}).get("category")
        if not cat:
            return []
        outcomes: list[Outcome] = []
        for e in cat.get("markets", {}).get("edges", []):
            m = e.get("node") or {}
            if m.get("status") == "RESOLVED":
                continue
            p = m.get("chancePercentage")
            if m.get("title") and p is not None:
                outcomes.append(Outcome(name=str(m["title"]),
                                        price=round(float(p) / 100.0, 4)))
        if not outcomes:
            return []
        return [Market(platform="predict", id=WINNER_CATEGORY,
                       title=cat.get("title") or "2026 World Cup Winner",
                       outcomes=outcomes,
                       url=f"https://predict.fun/c/{WINNER_CATEGORY}")]

    async def search_matches(self, limit: int = 80) -> list[Market]:
        from .polymarket import _split_vs
        data = await self._gql(
            '{categories(filter:{tag:"%s"},pagination:{first:100}){edges{node{'
            'id title startsAt status markets{edges{node{'
            'title chancePercentage status}}}}}}}' % WC_TAG)
        edges = ((data or {}).get("categories") or {}).get("edges", [])
        out: list[Market] = []
        for e in edges:
            c = e.get("node") or {}
            cid = c.get("id") or ""
            if not cid.startswith("fifwc-") or cid.endswith("more-markets"):
                continue
            title = (c.get("title") or "").replace(" vs. ", " vs ")
            sides = _split_vs(title)
            if not sides:
                continue
            subs = [s.get("node") or {} for s in
                    (c.get("markets") or {}).get("edges", [])]
            # 结果名是三字码：按位置映射回标题两队（非 Draw 的第 1/2 个 → A/B）
            outcomes: list[Outcome] = []
            non_draw_seen = 0
            for m in subs:
                p = m.get("chancePercentage")
                nm = str(m.get("title") or "").strip()
                if p is None or not nm:
                    continue
                if nm.lower() in ("draw", "tie"):
                    label = "Draw"
                else:
                    label = sides[0] if non_draw_seen == 0 else sides[1]
                    non_draw_seen += 1
                    if non_draw_seen > 2:
                        continue   # O/U 等杂项市场混进来则跳过
                outcomes.append(Outcome(name=label, price=round(float(p) / 100.0, 4)))
            if len(outcomes) < 2:
                continue
            out.append(Market(platform="predict", id=cid, title=title,
                              outcomes=outcomes,
                              url=f"https://predict.fun/c/{cid}",
                              close_ts=iso_to_ts(c.get("startsAt"))))
            if len(out) >= limit:
                break
        return out


class MockPredictClient(PredictionClient):
    platform = "predict"

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        from .model import matches
        data = [Market("predict", "pf-wc", "2026 World Cup Winner",
                       [Outcome("Spain", 0.18), Outcome("France", 0.17),
                        Outcome("Portugal", 0.12), Outcome("England", 0.11),
                        Outcome("Argentina", 0.10)])]
        return [m for m in data if matches(query, m.title, "world cup winner")][:limit]

    async def search_matches(self, limit: int = 40) -> list[Market]:
        import time
        now = time.time()
        return [Market("predict", "pf-kr-cze", "Korea Republic vs Czechia",
                       [Outcome("South Korea", 0.38), Outcome("Draw", 0.32),
                        Outcome("Czechia", 0.32)],
                       close_ts=now + 3600 * 10)][:limit]
