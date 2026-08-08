"""ZeroTrade 全体で共有するドメインモデル。

金額・数量・価格はすべて :class:`~decimal.Decimal` で扱う。
float の丸め誤差が証拠金計算やリスク判定に混入すると、
「1トレードあたり口座の1%」といった制約が静かに破られるため。

指標計算（SMA/RSI/ATR）だけは float を使い、
リスク判定に渡す境界で :func:`to_decimal` により Decimal へ戻す。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

__all__ = [
    "Balance",
    "Candle",
    "ClosedTrade",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "Side",
    "Signal",
    "SignalAction",
    "Ticker",
    "TimeInForce",
    "to_decimal",
    "utcnow",
]


def utcnow() -> datetime:
    """タイムゾーン付きの現在時刻（UTC）。

    naive datetime を混在させると日次損失のリセット判定がずれるため、
    システム内の時刻生成は必ずこの関数を経由する。
    """
    return datetime.now(UTC)


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """任意の数値を Decimal に正規化する。

    float は一度 str を経由することで ``0.1`` が
    ``0.1000000000000000055511151231257827`` になるのを防ぐ。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


class Side(StrEnum):
    """売買方向。"""

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> Decimal:
        """買い = +1 / 売り = -1。損益計算の符号として使う。"""
        return Decimal(1) if self is Side.BUY else Decimal(-1)


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    DAY = "day"


class OrderStatus(StrEnum):
    """注文のライフサイクル。"""

    PENDING = "pending"
    """ブローカーへ送信済みだが受理応答をまだ受け取っていない。"""

    OPEN = "open"
    """板に乗っている（未約定 or 部分約定）。"""

    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        """これ以上状態が変化しないか。"""
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """まだ約定しうるか（OrderManager の追跡対象か）。"""
        return not self.is_terminal


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)


class SignalAction(StrEnum):
    """戦略が出せる指示。"""

    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"

    UPDATE_STOP = "update_stop"
    """保有中の建玉のストップを引き上げる（トレーリングストップ）。

    順張りは少数の大きな勝ちで負けを賄う構造なので、
    利益が伸びた建玉のストップを追随させられないと利益が伸びない。
    引き上げ方向にしか動かせない（RiskManager が逆行を拒否する）。
    """

    HOLD = "hold"


def _new_client_id() -> str:
    """クライアント側で生成する冪等キー。

    再送時に同じIDを使うことで、ブローカー側の二重発注を検知できる。
    """
    return f"zt-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class Ticker:
    """気配値のスナップショット。"""

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def price_for(self, side: Side) -> Decimal:
        """その方向でエントリーする際に実際に払う側の価格。"""
        return self.ask if side is Side.BUY else self.bid


@dataclass(frozen=True, slots=True)
class Candle:
    """OHLCV の1本。"""

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)
    complete: bool = True


@dataclass(frozen=True, slots=True)
class Balance:
    """口座残高のスナップショット。"""

    currency: str
    equity: Decimal
    """有効証拠金（含み損益込みの純資産）。リスク計算の基準。"""

    available: Decimal
    """新規建てに使える余力。"""

    used_margin: Decimal = Decimal(0)
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def margin_usage_ratio(self) -> Decimal:
        """証拠金使用率（0.0〜1.0）。equity が 0 以下なら 1.0 とみなす。"""
        if self.equity <= 0:
            return Decimal(1)
        return self.used_margin / self.equity


@dataclass(frozen=True, slots=True)
class Position:
    """保有ポジション。"""

    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal = Decimal(0)
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    opened_at: datetime = field(default_factory=utcnow)
    broker_position_id: str | None = None

    def pnl_at(self, price: Decimal) -> Decimal:
        """指定価格での評価損益（建値通貨建て）。"""
        return (price - self.entry_price) * self.side.sign * self.quantity


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """決済済みトレード。日次・週次の損失集計はこれを基準に行う。

    含み損益ではなく **確定損益** だけを損失上限の判定に使う。
    含み損で停止すると、一時的な逆行で建玉を持ったまま
    新規も決済もできない状態に陥りうるため。
    """

    symbol: str
    side: Side
    """建玉の方向（決済注文の方向ではない）。"""

    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    """口座通貨建ての確定損益。損失は負値。"""

    opened_at: datetime
    closed_at: datetime = field(default_factory=utcnow)
    trade_id: str = ""
    reason: str = ""
    """``stop_loss`` / ``take_profit`` / ``signal`` / ``manual`` など。"""


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """発注リクエスト。RiskManager が検査する対象。

    ブローカーへ渡す前に必ず :meth:`RiskManager.evaluate` を通す。
    """

    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    """決済専用注文。新規リスクを増やさないためリスク検査の一部を免除する。"""

    client_order_id: str = field(default_factory=_new_client_id)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity は正の数である必要があります: {self.quantity}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("指値注文には limit_price が必要です")
        if self.order_type is OrderType.STOP and self.stop_price is None:
            raise ValueError("逆指値注文には stop_price が必要です")


@dataclass(slots=True)
class Order:
    """ブローカーに受理された注文の現在状態。"""

    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    reduce_only: bool = False
    """決済専用。対象の建玉が無い場合、新規建てになってはならない。"""

    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    reject_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal(0), self.quantity - self.filled_quantity)

    @property
    def is_active(self) -> bool:
        return self.status.is_active


@dataclass(frozen=True, slots=True)
class Signal:
    """戦略の出力。

    戦略は「何をしたいか」だけを述べ、
    サイズ決定は PositionSizer、可否判断は RiskManager が行う。
    """

    symbol: str
    action: SignalAction
    strategy: str = "unknown"
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    confidence: Decimal = Decimal(1)
    reason: str = ""
    timestamp: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)

    @property
    def side(self) -> Side | None:
        """エントリーシグナルなら対応する売買方向。それ以外は None。"""
        if self.action is SignalAction.ENTER_LONG:
            return Side.BUY
        if self.action is SignalAction.ENTER_SHORT:
            return Side.SELL
        return None
