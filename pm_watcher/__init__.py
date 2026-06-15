"""pm_watcher 包：按平台名拿只读 client（live 用真实公开端点，否则 mock）。"""
from .model import Market, Outcome, PredictionClient
from .polymarket import PolymarketClient, MockPolymarketClient
from .kalshi import KalshiClient, MockKalshiClient
from .fortytwo import FortyTwoClient, MockFortyTwoClient
from .manifold import ManifoldClient, MockManifoldClient
from .predict import PredictClient, MockPredictClient

# 都是公开只读端点，live 直接用、无需任何 key。
# manifold 是 play-money 预测者共识；predict 是 BNB 链现金盘（前端公开 GraphQL）；其余是现金盘 / 链上盘。
# （metaculus 适配器仍保留在 metaculus.py，其 API 现需注册 token，故不在默认列表。）
_LIVE = {
    "polymarket": PolymarketClient,
    "kalshi": KalshiClient,
    "42": FortyTwoClient,   # 真实只读端点 rest.ft.42.space
    "manifold": ManifoldClient,
    "predict": PredictClient,
}
_MOCK = {
    "polymarket": MockPolymarketClient,
    "kalshi": MockKalshiClient,
    "42": MockFortyTwoClient,
    "manifold": MockManifoldClient,
    "predict": MockPredictClient,
}

ALL_PLATFORMS = list(_MOCK.keys())


def make_client(platform: str, live: bool) -> PredictionClient:
    table = _LIVE if live else _MOCK
    if platform not in table:
        raise ValueError(f"未知平台: {platform}（可选: {', '.join(ALL_PLATFORMS)}）")
    return table[platform]()


__all__ = ["Market", "Outcome", "PredictionClient", "make_client", "ALL_PLATFORMS"]
