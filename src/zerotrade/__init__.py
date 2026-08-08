"""ZeroTrade — ルール徹底とリスク管理を強制する自動売買ベースシステム。

設計の芯:

* 戦略はシグナルを出すだけで、発注はできない
* すべての新規注文は RiskManager を必ず通る
* 日次・週次の損失上限に達したら自動で停止し、決済だけを許す
* ブローカー固有の仕様は Adapter の内側に閉じ込める
"""

from __future__ import annotations

from zerotrade.app import Application, build_application
from zerotrade.errors import (
    BrokerError,
    ConfigError,
    RiskViolation,
    TradingHalted,
    ZeroTradeError,
)
from zerotrade.models import (
    Balance,
    Candle,
    ClosedTrade,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Signal,
    SignalAction,
    Ticker,
)
from zerotrade.settings import Settings, load_settings

__version__ = "0.1.0"

__all__ = [
    "Application",
    "Balance",
    "BrokerError",
    "Candle",
    "ClosedTrade",
    "ConfigError",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "RiskViolation",
    "Settings",
    "Side",
    "Signal",
    "SignalAction",
    "Ticker",
    "TradingHalted",
    "ZeroTradeError",
    "__version__",
    "build_application",
    "load_settings",
]
