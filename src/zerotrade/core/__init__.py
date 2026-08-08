"""コア層: リスク管理・サイズ決定・注文管理・実行ループ・通知。"""

from __future__ import annotations

from zerotrade.core.notifier import Notifier, build_notifier
from zerotrade.core.orders import OrderManager, SubmitResult
from zerotrade.core.risk import MarketContext, RiskDecision, RiskManager, RiskState
from zerotrade.core.runner import StrategyRunner
from zerotrade.core.sizing import PositionSizer, SizingResult

__all__ = [
    "MarketContext",
    "Notifier",
    "OrderManager",
    "PositionSizer",
    "RiskDecision",
    "RiskManager",
    "RiskState",
    "SizingResult",
    "StrategyRunner",
    "SubmitResult",
    "build_notifier",
]
