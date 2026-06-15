"""
Metaculus 只读适配器（公开 API、研究型社区预测）。

冠军问题：GET https://www.metaculus.com/api2/questions/43310/
  "Who will win the 2026 FIFA World Cup?"（multiple choice）

注意：
- Metaculus 是预测社区，不是交易市场；该问题预测者很少（~13 人），信号偏薄，当参考。
- API 返回结构有过几版（question 包在 post 里 / 直接在顶层；概率在
  aggregations.recency_weighted.latest 的 forecast_values 数组，或
  probability_yes_per_category 字典）。这里做防御式解析：两种都试，
  解析不出就返回空，不污染榜单。
"""
from __future__ import annotations

import httpx

from .model import Market, Outcome, PredictionClient

BASE = "https://www.metaculus.com/api2"

KNOWN_QUESTIONS: dict[str, int] = {
    "world cup": 43310,   # Who will win the 2026 FIFA World Cup?
}


class MetaculusClient(PredictionClient):
    platform = "metaculus"

    def __init__(self, base: str = BASE) -> None:
        import os
        headers = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json",
        }
        # Metaculus API 现已要求鉴权：在 .env 配 METACULUS_TOKEN= 后才会有数据。
        from . import config  # 触发 .env 加载
        tok = os.getenv("METACULUS_TOKEN", "").strip()
        if tok:
            headers["Authorization"] = f"Token {tok}"
        self._has_token = bool(tok)
        self._http = httpx.AsyncClient(base_url=base, timeout=20.0, headers=headers)

    async def close(self) -> None:
        await self._http.aclose()

    async def search_markets(self, query: str, limit: int = 64) -> list[Market]:
        q = query.lower()
        qid = next((v for k, v in KNOWN_QUESTIONS.items() if k in q), None)
        if not qid:
            return []
        # Metaculus 2024+ 把问题包成 post：新端点 /api/posts/{id}/ 优先，旧 /api2 兜底
        data = None
        for path in (f"https://www.metaculus.com/api/posts/{qid}/",
                     f"{BASE}/questions/{qid}/"):
            try:
                r = await self._http.get(path)
                if r.status_code == 200:
                    data = r.json()
                    break
            except Exception:
                continue
        if not data:
            return []
        # 形状 A：post 包着 question；形状 B：question 即顶层
        ques = data.get("question") or data
        title = data.get("title") or ques.get("title") or "Metaculus"
        options = ques.get("options") or []
        # 选项可能是字符串列表，也可能是 [{"label": ...}] 之类
        names = []
        for o in options:
            if isinstance(o, str):
                names.append(o)
            elif isinstance(o, dict):
                names.append(str(o.get("label") or o.get("name") or o.get("text") or ""))
        agg = (((ques.get("aggregations") or {}).get("recency_weighted") or {})
               .get("latest") or {})
        vals = agg.get("forecast_values") or []
        probs: dict[str, float] = {}
        if names and vals and len(vals) == len(names):
            probs = {n: float(v) for n, v in zip(names, vals)}
        else:
            # 备选形状：probability_yes_per_category 是 {选项名: 概率}
            pypc = agg.get("probability_yes_per_category") or {}
            if isinstance(pypc, dict) and pypc:
                probs = {str(k): float(v) for k, v in pypc.items()}
        if not probs:
            return []   # 结构对不上 → 安静返回空，不污染榜单
        outcomes = [Outcome(name=n, price=p) for n, p in probs.items()
                    if n and not n.lower().startswith("other")]
        return [Market(platform="metaculus", id=str(qid), title=title,
                       outcomes=outcomes,
                       url=f"https://www.metaculus.com/questions/{qid}/")]


class MockMetaculusClient(PredictionClient):
    platform = "metaculus"

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        from .model import matches
        data = [Market("metaculus", "mc-wc", "Who will win the 2026 FIFA World Cup?",
                       [Outcome("France", 0.197), Outcome("Spain", 0.161),
                        Outcome("Brazil", 0.102), Outcome("Argentina", 0.10)])]
        return [m for m in data if matches(query, m.title, "world cup winner")][:limit]
