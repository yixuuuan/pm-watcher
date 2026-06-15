"""
Kalshi 只读适配器。

行情走 trade-api v2：https://api.elections.kalshi.com/trade-api/v2 —— 读取端点公开、无需 key。
（下单才需要鉴权：RSA 签名 / 会话 token，本版不涉及。）

要点：
- /markets 返回 {"markets":[...], "cursor": "..."}，可用 cursor 翻页。
- 价格是 0~100 美分；这里 /100 归一化为 0~1。
- 没有官方全文搜索，这里翻几页活跃市场后在客户端按 title 过滤。
- 二元市场：用 last_price（无则用 yes_bid/yes_ask 中值）当 Yes 概率，No = 1 - Yes。
"""
from __future__ import annotations

import httpx

from .model import Market, Outcome, PredictionClient, iso_to_ts, matches

BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient(PredictionClient):
    platform = "kalshi"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=BASE, timeout=20.0,
                                       headers={"User-Agent": "pm-watcher/0.1"})

    async def close(self) -> None:
        await self._http.aclose()

    # 已知"关键词 → Kalshi 事件票据"，命中后直接定位，避免在上千市场里盲翻
    KNOWN_EVENTS = {
        "world cup": "KXMENWORLDCUP-26",
    }
    # 关键词 → 系列票据（一个系列下多个事件，如 12 个小组）。更具体的键放前面。
    KNOWN_SERIES = {
        "world cup group": "KXWCGROUPWIN",   # Group A–L Winner（实地核实）
    }
    MATCH_SERIES = "KXWCGAME"   # 单场胜负：事件题 "Jordan vs Argentina"，结果含 Tie

    async def _events_nested(self, series: str, limit: int = 100) -> list[dict]:
        r = await self._http.get("/events", params={
            "series_ticker": series, "status": "open",
            "with_nested_markets": "true", "limit": limit})
        r.raise_for_status()
        return r.json().get("events", [])

    @staticmethod
    def _nested_outcomes(ev: dict) -> tuple[list[Outcome], float | None]:
        outs: list[Outcome] = []
        close = None
        for m in ev.get("markets", []):
            if m.get("status") not in ("active", "open"):
                continue
            nm = (m.get("yes_sub_title") or m.get("ticker") or "").strip()
            p = m.get("last_price_dollars")
            if p is None and m.get("last_price") is not None:
                p = float(m["last_price"]) / 100.0
            if nm and p is not None:
                outs.append(Outcome(name=nm, price=float(p)))
            ct = iso_to_ts(m.get("close_time"))
            if ct and (close is None or ct < close):
                close = ct
        return outs, close

    async def search_matches(self, limit: int = 80) -> list[Market]:
        out: list[Market] = []
        for ev in await self._events_nested(self.MATCH_SERIES, limit=limit):
            title = (ev.get("title") or "").strip()
            outs, close = self._nested_outcomes(ev)
            # Kalshi 用 "Tie" 表示平局 → 统一成 Draw
            outs = [Outcome(name=("Draw" if o.name.lower() == "tie" else o.name),
                            price=o.price) for o in outs]
            if len(outs) < 2 or " vs" not in title.lower():
                continue
            tk = ev.get("event_ticker", "")
            out.append(Market(platform="kalshi", id=tk, title=title, outcomes=outs,
                              url=f"https://kalshi.com/markets/{tk}" if tk else None,
                              close_ts=close))
        return out

    async def search_markets(self, query: str, limit: int = 64,
                             max_pages: int = 6) -> list[Market]:
        q = query.lower()

        # 路径 0：命中已知系列（如小组第一）→ 每个事件出一个多结果市场
        series = next((tk for kw, tk in self.KNOWN_SERIES.items() if kw in q), None)
        if series:
            out: list[Market] = []
            for ev in await self._events_nested(series):
                outs, close = self._nested_outcomes(ev)
                if not outs:
                    continue
                tk = ev.get("event_ticker", "")
                out.append(Market(platform="kalshi", id=tk,
                                  title=ev.get("title") or tk, outcomes=outs,
                                  url=f"https://kalshi.com/markets/{tk}" if tk else None,
                                  close_ts=close))
                if len(out) >= limit:
                    break
            return out

        event = next((tk for kw, tk in self.KNOWN_EVENTS.items() if kw in q), None)

        # 路径 A：命中已知事件 → 直接按 event_ticker 拉该事件全部市场（最可靠）
        if event:
            r = await self._http.get(
                "/markets", params={"event_ticker": event, "limit": 1000})
            r.raise_for_status()
            out: list[Market] = []
            for m in r.json().get("markets", []):
                if m.get("status") in ("settled", "finalized", "closed", "determined"):
                    continue
                out.append(self._parse(m))
                if len(out) >= limit:
                    break
            return out

        # 路径 B：通用关键词扫描（翻页 + 客户端过滤）
        out = []
        cursor = None
        for _ in range(max_pages):
            params = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = await self._http.get("/markets", params=params)
            r.raise_for_status()
            data = r.json()
            for m in data.get("markets", []):
                if m.get("status") not in ("active", "open"):
                    continue
                title = m.get("title") or m.get("yes_sub_title") or ""
                if not matches(query, title, m.get("ticker", "")):
                    continue
                out.append(self._parse(m))
                if len(out) >= limit:
                    return out
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    @staticmethod
    def _parse(m: dict) -> Market:
        yes = _yes_prob(m)
        outcomes = [Outcome("Yes", yes),
                    Outcome("No", None if yes is None else round(1 - yes, 4))]
        ticker = m.get("ticker", "")
        # 在"分组事件"里（如世界杯冠军），队名通常在 yes_sub_title；用作跨平台对齐锚点
        team = (m.get("yes_sub_title") or "").strip()
        return Market(
            platform="kalshi",
            id=ticker,
            title=m.get("title") or team or ticker,
            outcomes=outcomes,
            url=f"https://kalshi.com/markets/{ticker}" if ticker else None,
            close_ts=iso_to_ts(m.get("close_time")),
            volume=_to_float(m.get("volume_fp") or m.get("volume")),
            group=team or None,
        )


class MockKalshiClient(PredictionClient):
    platform = "kalshi"

    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        data = [
            Market("kalshi", "KXWCUP-26-ESP", "Will Spain win the 2026 World Cup?",
                   [Outcome("Yes", 0.21), Outcome("No", 0.79)],
                   url="https://kalshi.com/markets/KXWCUP-26-ESP"),
            Market("kalshi", "KXWCUP-26-ARG", "Will Argentina win the 2026 World Cup?",
                   [Outcome("Yes", 0.17), Outcome("No", 0.83)]),
        ]
        return [m for m in data if matches(query, m.title)][:limit]


def _yes_prob(m: dict) -> float | None:
    # 现行 API：*_dollars 是 0~1 的美元字符串（不要再 /100）
    lp = _to_float(m.get("last_price_dollars"))
    if lp and lp > 0:
        return round(lp, 4)
    bid = _to_float(m.get("yes_bid_dollars"))
    ask = _to_float(m.get("yes_ask_dollars"))
    if bid is not None and ask is not None and (bid or ask):
        return round((bid + ask) / 2, 4)
    # 兼容旧版美分字段
    lp_c = m.get("last_price")
    if isinstance(lp_c, (int, float)) and lp_c > 0:
        return round(lp_c / 100.0, 4)
    return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
