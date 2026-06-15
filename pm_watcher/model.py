"""
统一数据模型 + 客户端接口。所有平台适配器都归一化到这里的 Market / Outcome，
聚合器和上层只认这套结构，与各家原始字段解耦。

价格约定：全部归一化为 0~1 的隐含概率（Kalshi 的美分会除以 100）。
本版只读：只有 search_markets，没有下单。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Outcome:
    name: str                  # 例如 "Spain" / "Yes" / "西班牙夺冠"
    price: float | None        # 0~1 隐含概率；拿不到时 None


@dataclass
class Market:
    platform: str              # "polymarket" / "kalshi" / "42"
    id: str                    # 平台内市场标识（slug / ticker / id）
    title: str
    outcomes: list[Outcome] = field(default_factory=list)
    url: str | None = None
    close_ts: float | None = None
    volume: float | None = None
    group: str | None = None   # 二元市场所属的"结果名"，如 Polymarket 的 groupItemTitle="Spain"

    def outcome(self, name_substr: str) -> Outcome | None:
        """按名字模糊找一个结果（大小写不敏感、子串匹配）。"""
        s = name_substr.lower()
        for o in self.outcomes:
            if s in o.name.lower():
                return o
        return None


class PredictionClient(ABC):
    platform: str = "base"

    @abstractmethod
    async def search_markets(self, query: str, limit: int = 10) -> list[Market]:
        """按关键词返回归一化后的市场列表。"""
        ...

    async def search_matches(self, limit: int = 40) -> list[Market]:
        """返回"单场比赛"市场（title 形如 'A vs B'，outcomes 为 A/Draw/B 或 A/B）。
        默认空：尚未接比赛盘的平台返回 []，不污染赛程。"""
        return []

    async def close(self) -> None:
        return None


# --- 小工具 ---
def iso_to_ts(v) -> float | None:
    if not v:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def matches(query: str, *texts: str) -> bool:
    """query 中每个词都出现在拼接文本里才算命中（粗匹配）。"""
    blob = " ".join(t.lower() for t in texts if t)
    return all(tok in blob for tok in query.lower().split())
