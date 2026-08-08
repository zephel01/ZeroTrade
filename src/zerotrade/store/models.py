"""記録層が返す読み取り用の行モデル。

ドメインモデル（:mod:`zerotrade.models`）とは意図的に分けてある。
こちらは「保存されたものを読み出した結果」であり、
戦略や発注に使うことは想定していない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = [
    "EquityPoint",
    "EventRow",
    "PerformanceSummary",
    "RejectionRow",
    "SignalRow",
    "TradeRow",
]


@dataclass(frozen=True, slots=True)
class TradeRow:
    """決済済みトレード1件。"""

    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    opened_at: datetime
    closed_at: datetime
    reason: str
    strategy: str

    @property
    def is_win(self) -> bool:
        return self.realized_pnl > 0


@dataclass(frozen=True, slots=True)
class SignalRow:
    symbol: str
    action: str
    strategy: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RejectionRow:
    """リスク検査で却下された発注。

    「なぜ発注されなかったか」を後から追える唯一の記録なので、
    トレード履歴と同じ重みで残す。
    """

    symbol: str
    side: str
    quantity: Decimal
    rule: str
    detail: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EquityPoint:
    created_at: datetime
    equity: Decimal
    used_margin: Decimal
    open_positions: int


@dataclass(frozen=True, slots=True)
class EventRow:
    """起動・停止・取引停止など、運用上の節目。"""

    kind: str
    detail: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    """トレード履歴から算出した成績。

    ``trades`` が 0 のときは全項目が 0 になる。
    「まだ判断材料が無い」ことを NaN ではなく 0 で表す。
    """

    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: Decimal = Decimal(0)
    gross_loss: Decimal = Decimal(0)
    """負けトレードの損失合計（正の値で保持）。"""

    net_pnl: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    """確定損益の累積曲線における最大の落ち込み（正の値）。"""

    @property
    def win_rate(self) -> Decimal:
        if self.trades == 0:
            return Decimal(0)
        return Decimal(self.wins) / Decimal(self.trades)

    @property
    def average_win(self) -> Decimal:
        if self.wins == 0:
            return Decimal(0)
        return self.gross_profit / Decimal(self.wins)

    @property
    def average_loss(self) -> Decimal:
        if self.losses == 0:
            return Decimal(0)
        return self.gross_loss / Decimal(self.losses)

    @property
    def profit_factor(self) -> Decimal | None:
        """総利益 ÷ 総損失。負けが1件も無ければ None（値が定義できない）。"""
        if self.gross_loss == 0:
            return None
        return self.gross_profit / self.gross_loss

    @property
    def expectancy(self) -> Decimal:
        """1トレードあたりの期待損益。"""
        if self.trades == 0:
            return Decimal(0)
        return self.net_pnl / Decimal(self.trades)
