"""ZeroTrade の例外階層。

方針: 「注文が通らなかった」ことは呼び出し側が必ず気づける形にする。
戻り値を無視して発注が素通りする事故を防ぐため、
リスク違反は :class:`RiskViolation` として送出できるようにしてある
（既定の実行経路では :class:`~zerotrade.core.risk.RiskDecision` を返し、
StrategyRunner が明示的に握りつぶす）。
"""

from __future__ import annotations

__all__ = [
    "BrokerError",
    "ConfigError",
    "InsufficientFundsError",
    "OrderRejected",
    "RiskViolation",
    "TradingHalted",
    "ZeroTradeError",
]


class ZeroTradeError(Exception):
    """すべての ZeroTrade 例外の基底。"""


class ConfigError(ZeroTradeError):
    """設定ファイル・環境変数の不備。"""


class BrokerError(ZeroTradeError):
    """ブローカーとの通信・API レベルの失敗。"""


class OrderRejected(BrokerError):
    """ブローカー側で注文が拒否された。"""

    def __init__(self, message: str, *, client_order_id: str | None = None) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id


class InsufficientFundsError(BrokerError):
    """余力不足。"""


class RiskViolation(ZeroTradeError):
    """RiskManager のルールに違反した。

    Attributes:
        rule: 違反したルール名（例 ``"max_risk_per_trade"``）。
        detail: 人間が読める説明。通知にそのまま載せる想定。
    """

    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"[{rule}] {detail}")
        self.rule = rule
        self.detail = detail


class TradingHalted(RiskViolation):
    """日次/週次の最大損失に達し、取引が自動停止している。"""
