"""PaperBroker — 約定シミュレータ。

外部APIに一切触れずにシステム全体（シグナル → リスク検査 → 発注 →
ストップ/利確 → 損益確定）を通しで動かすための実装。

シミュレーションの前提:

* 成行は現在の bid/ask で即時全量約定する（スリッページは設定可能）。
* 指値・逆指値は、足が進んだときに高値/安値がトリガー価格へ届いたら約定する。
* ストップ・利確は足の高値/安値で判定する。同じ足で両方に触れた場合は
  **ストップ側を優先** する（楽観的なバックテスト結果を避けるため）。
* 銘柄ごとにネットの建玉を1つだけ持つ（両建てしない）。

価格系列は :func:`~zerotrade.data.historical.synthetic_candles` による疑似データ、
または外部から与えたローソク足を使う。時間は :meth:`get_ohlcv` の呼び出しごとに
1本進む（StrategyRunner が銘柄あたり毎ループ1回呼ぶことを前提とした設計）。

**時刻はすべて足の時刻を使う**（実時間ではない）。バックテストで
2年ぶんを数秒で流したとき、実時間を使うと日次損失上限が一度も
リセットされず、最初の停止でそのまま終わってしまうため。
:attr:`simulated_time` がこのブローカーにとっての「今」になる。
"""

from __future__ import annotations

import itertools
from datetime import datetime
from decimal import Decimal

from zerotrade.brokers.base import BaseBroker
from zerotrade.core.excursion import ExcursionTracker
from zerotrade.data.historical import synthetic_candles
from zerotrade.errors import BrokerError, InsufficientFundsError, OrderRejected
from zerotrade.log import get_logger
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
    Ticker,
    to_decimal,
    utcnow,
)

__all__ = ["PaperBroker"]

logger = get_logger(__name__)


class PaperBroker(BaseBroker):
    """ペーパートレード用の擬似ブローカー。"""

    name = "paper"
    supports_closed_trades = True

    # 注文は手元で完結し、外へ出ない。
    is_simulated = True

    def __init__(
        self,
        symbols: list[str],
        *,
        initial_balance: Decimal = Decimal(1_000_000),
        currency: str = "JPY",
        spread: Decimal = Decimal("0.02"),
        leverage: Decimal = Decimal(25),
        slippage: Decimal = Decimal(0),
        contract_size: Decimal = Decimal(1),
        candles: dict[str, list[Candle]] | None = None,
        warmup_bars: int = 120,
        seed: int = 42,
    ) -> None:
        if not symbols:
            raise ValueError("symbols を1つ以上指定してください")

        self._symbols = list(symbols)
        self._currency = currency
        self._spread = spread
        self._leverage = leverage
        self._slippage = slippage
        self._contract_size = contract_size

        self._cash = initial_balance
        self._initial_balance = initial_balance

        self._candles: dict[str, list[Candle]] = candles or {
            # 銘柄ごとに seed をずらし、全通貨が同じ動きをする不自然さを避ける。
            symbol: synthetic_candles(symbol, count=1000, seed=seed + i)
            for i, symbol in enumerate(self._symbols)
        }
        missing = [s for s in self._symbols if not self._candles.get(s)]
        if missing:
            raise ValueError(f"価格データが無い銘柄があります: {', '.join(missing)}")

        # ウォームアップぶんの足は最初から見えている状態にする。
        self._cursor: dict[str, int] = {
            symbol: min(warmup_bars, len(self._candles[symbol])) for symbol in self._symbols
        }

        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._closed_trades: list[ClosedTrade] = []
        # 足の高値・安値で MFE/MAE を測る。気配値の標本より正確になる。
        self._excursions = ExcursionTracker(contract_size)
        self._connected = False
        self._id_counter = itertools.count(1)

    # ------------------------------------------------------------ 接続

    async def connect(self) -> None:
        self._connected = True
        logger.info(
            "PaperBroker に接続しました（初期残高 %s %s / 銘柄 %s）",
            self._initial_balance,
            self._currency,
            ", ".join(self._symbols),
        )

    async def disconnect(self) -> None:
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerError("PaperBroker が未接続です。connect() を呼んでください")

    # ------------------------------------------------------------ 時計

    @property
    def simulated_time(self) -> datetime:
        """このブローカーにとっての「今」。見えている足のうち最も新しい時刻。

        RiskManager の clock にこれを渡すと、日次・週次のリセットが
        実時間ではなく相場時間で起きるようになる。
        """
        times = [self._current_candle(symbol).timestamp for symbol in self._symbols]
        return max(times) if times else utcnow()

    # ------------------------------------------------------------ 口座

    async def get_balance(self) -> Balance:
        self._require_connected()
        unrealized = sum(
            (self._unrealized(position) for position in self._positions.values()),
            Decimal(0),
        )
        equity = self._cash + unrealized
        used_margin = sum(
            (self._margin_for(position) for position in self._positions.values()),
            Decimal(0),
        )
        return Balance(
            currency=self._currency,
            equity=equity,
            available=max(Decimal(0), equity - used_margin),
            used_margin=used_margin,
            timestamp=self.simulated_time,
        )

    async def get_positions(self) -> list[Position]:
        self._require_connected()
        return [
            Position(
                symbol=p.symbol,
                side=p.side,
                quantity=p.quantity,
                entry_price=p.entry_price,
                unrealized_pnl=self._unrealized(p),
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                opened_at=p.opened_at,
                broker_position_id=p.broker_position_id,
            )
            for p in self._positions.values()
        ]

    async def update_position_stop(self, symbol: str, stop_loss: Decimal) -> Position | None:
        """建玉のストップを更新する。

        **不利な方向へは動かさない。** 買い建玉のストップを下げる操作は
        損失許容量を後から広げる行為で、リスク管理の前提が崩れる。
        """
        self._require_connected()
        position = self._positions.get(symbol)
        if position is None:
            return None

        if position.stop_loss is not None:
            worsens = (
                stop_loss < position.stop_loss
                if position.side is Side.BUY
                else stop_loss > position.stop_loss
            )
            if worsens:
                logger.debug(
                    "%s のストップ引き下げ要求を無視しました（%s → %s）",
                    symbol,
                    position.stop_loss,
                    stop_loss,
                )
                return position

        updated = Position(
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            stop_loss=stop_loss,
            take_profit=position.take_profit,
            opened_at=position.opened_at,
            broker_position_id=position.broker_position_id,
        )
        self._positions[symbol] = updated
        return updated

    async def get_closed_trades(self, since: datetime | None = None) -> list[ClosedTrade]:
        if since is None:
            return list(self._closed_trades)
        return [t for t in self._closed_trades if t.closed_at >= since]

    # ------------------------------------------------------------ 相場

    async def get_ticker(self, symbol: str) -> Ticker:
        self._require_connected()
        candle = self._current_candle(symbol)
        half = self._spread / 2
        return Ticker(
            symbol=symbol,
            bid=candle.close - half,
            ask=candle.close + half,
            timestamp=candle.timestamp,
        )

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        """見えている範囲の足を返し、同時に時計を1本進める。

        時間を進めた結果としてストップ・利確・指値がヒットしていれば
        この中で約定処理まで済ませる。
        """
        self._require_connected()
        self._advance(symbol)
        series = self._series(symbol)
        cutoff = self._cursor[symbol]
        if end is not None:
            # 疑似ブローカーでも遡り取得の意味は保つ。
            cutoff = min(cutoff, sum(1 for c in series if c.timestamp < end))
        return list(series[max(0, cutoff - count) : cutoff])

    # ------------------------------------------------------------ 注文

    async def place_order(self, request: OrderRequest) -> Order:
        self._require_connected()
        if request.symbol not in self._candles:
            raise OrderRejected(
                f"未知の銘柄です: {request.symbol}", client_order_id=request.client_order_id
            )

        order = Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            broker_order_id=f"paper-{next(self._id_counter)}",
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            reduce_only=request.reduce_only,
            reference_price=request.reference_price,
            metadata=dict(request.metadata),
            created_at=self.simulated_time,
            updated_at=self.simulated_time,
        )

        if request.order_type is OrderType.MARKET:
            ticker = await self.get_ticker(request.symbol)
            price = ticker.price_for(request.side)
            # スリッページは常に不利な方向へ乗せる。
            price += self._slippage * request.side.sign
            self._fill(order, price, reason="signal" if not request.reduce_only else "exit")
        else:
            order.status = OrderStatus.OPEN

        self._orders[order.client_order_id] = order
        return order

    async def cancel_order(self, order_id: str) -> Order:
        self._require_connected()
        order = self._find_order(order_id)
        if order.status.is_terminal:
            return order
        order.status = OrderStatus.CANCELLED
        order.updated_at = self.simulated_time
        return order

    async def get_order(self, order_id: str) -> Order:
        self._require_connected()
        return self._find_order(order_id)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        self._require_connected()
        return [
            order
            for order in self._orders.values()
            if order.is_active and (symbol is None or order.symbol == symbol)
        ]

    # ------------------------------------------------------ 内部: 時間を進める

    def _advance(self, symbol: str) -> None:
        """足を1本進め、その足の値幅で執行判定を行う。"""
        series = self._series(symbol)
        if self._cursor[symbol] >= len(series):
            # データを使い切った。以降は最後の足に張り付く。
            return
        self._cursor[symbol] += 1
        candle = series[self._cursor[symbol] - 1]
        self._process_pending_orders(symbol, candle)
        # 決済判定より前に測る。ストップに掛かった足の逆行も MAE に含めたい。
        self._track_excursion(symbol, candle)
        self._process_exits(symbol, candle)

    def _track_excursion(self, symbol: str, candle: Candle) -> None:
        """この足の高値・安値で建玉の含み損益の振れ幅を更新する。"""
        position = self._positions.get(symbol)
        if position is not None:
            self._excursions.observe_range(position, candle.high, candle.low)

    def _process_pending_orders(self, symbol: str, candle: Candle) -> None:
        """指値・逆指値がこの足の値幅に触れていれば約定させる。"""
        for order in list(self._orders.values()):
            if order.symbol != symbol or not order.is_active:
                continue
            trigger = order.limit_price if order.order_type is OrderType.LIMIT else order.stop_price
            if trigger is None:
                continue

            if order.order_type is OrderType.LIMIT:
                # 買い指値は安値が届けば、売り指値は高値が届けば約定。
                hit = candle.low <= trigger if order.side is Side.BUY else candle.high >= trigger
            else:
                # 逆指値は逆方向。
                hit = candle.high >= trigger if order.side is Side.BUY else candle.low <= trigger

            if hit:
                self._fill(order, trigger, reason="signal")

    def _process_exits(self, symbol: str, candle: Candle) -> None:
        """建玉のストップ・利確判定。同足で両方触れたらストップ優先。"""
        position = self._positions.get(symbol)
        if position is None:
            return

        if position.side is Side.BUY:
            stop_hit = position.stop_loss is not None and candle.low <= position.stop_loss
            target_hit = position.take_profit is not None and candle.high >= position.take_profit
        else:
            stop_hit = position.stop_loss is not None and candle.high >= position.stop_loss
            target_hit = position.take_profit is not None and candle.low <= position.take_profit

        if stop_hit:
            assert position.stop_loss is not None
            self._close_position(symbol, position.stop_loss, reason="stop_loss")
        elif target_hit:
            assert position.take_profit is not None
            self._close_position(symbol, position.take_profit, reason="take_profit")

    # ------------------------------------------------------ 内部: 約定処理

    def _fill(self, order: Order, price: Decimal, *, reason: str) -> None:
        """注文を約定させ、建玉へ反映する。"""
        existing = self._positions.get(order.symbol)

        if order.reduce_only:
            # 決済専用の注文が新規建てになってはならない。
            # ストップに掛かった直後など、既に建玉が消えている場面で
            # これを許すと、意図しない反対方向のポジションが生まれる。
            if existing is None or existing.side is order.side:
                order.status = OrderStatus.REJECTED
                order.reject_reason = "決済対象の建玉がありません"
                order.updated_at = self.simulated_time
                raise OrderRejected(
                    f"{order.symbol}: {order.reject_reason}",
                    client_order_id=order.client_order_id,
                )
            # 建玉より多い決済数量は、余剰ぶんが新規建てになる。切り詰める。
            closing = min(existing.quantity, order.quantity)
            self._reduce_position(order.symbol, closing, price, reason=reason)
            order.status = OrderStatus.FILLED
            order.filled_quantity = closing
            order.average_price = price
            order.updated_at = self.simulated_time
            return

        if existing is not None and existing.side is not order.side:
            closing = min(existing.quantity, order.quantity)
            self._reduce_position(order.symbol, closing, price, reason=reason)
            remaining = order.quantity - closing
            if remaining > 0:
                self._open_position(order, remaining, price)
        else:
            required = self._required_margin(price, order.quantity)
            available = self._available_margin()
            if required > available:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"証拠金不足（必要 {required:.2f} / 余力 {available:.2f}）"
                order.updated_at = self.simulated_time
                raise InsufficientFundsError(order.reject_reason)
            self._open_position(order, order.quantity, price)

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_price = price
        order.updated_at = self.simulated_time

    def _open_position(self, order: Order, quantity: Decimal, price: Decimal) -> None:
        """新規建て、または同方向への積み増し（平均建値を更新）。"""
        existing = self._positions.get(order.symbol)
        if existing is None:
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                side=order.side,
                quantity=quantity,
                entry_price=price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                opened_at=self.simulated_time,
                broker_position_id=order.broker_order_id,
            )
            return

        total = existing.quantity + quantity
        average = (existing.entry_price * existing.quantity + price * quantity) / total
        self._positions[order.symbol] = Position(
            symbol=existing.symbol,
            side=existing.side,
            quantity=total,
            entry_price=average,
            stop_loss=order.stop_loss or existing.stop_loss,
            take_profit=order.take_profit or existing.take_profit,
            opened_at=existing.opened_at,
            broker_position_id=existing.broker_position_id,
        )

    def _reduce_position(
        self, symbol: str, quantity: Decimal, price: Decimal, *, reason: str
    ) -> None:
        """建玉を部分・全部決済し、確定損益を計上する。"""
        position = self._positions[symbol]
        pnl = (price - position.entry_price) * position.side.sign * quantity * self._contract_size
        self._cash += pnl

        # 部分決済では、建玉全体で測った振れ幅を決済した割合で按分する。
        ratio = quantity / position.quantity if position.quantity > 0 else Decimal(1)
        excursion = self._excursions.snapshot(symbol, ratio=ratio)
        # 決済価格そのものが最大値を更新していることがある（ストップ・利確）。
        if excursion is not None:
            excursion = excursion.extended(pnl, pnl)

        self._closed_trades.append(
            ClosedTrade(
                symbol=symbol,
                side=position.side,
                quantity=quantity,
                entry_price=position.entry_price,
                exit_price=price,
                realized_pnl=pnl,
                opened_at=position.opened_at,
                closed_at=self.simulated_time,
                trade_id=position.broker_position_id or symbol,
                reason=reason,
                mfe=None if excursion is None else excursion.favorable,
                mae=None if excursion is None else excursion.adverse,
            )
        )
        logger.info(
            "決済しました: %s %s %s @ %s（損益 %+.2f / %s）",
            symbol,
            position.side,
            quantity,
            price,
            pnl,
            reason,
            extra={"symbol": symbol, "pnl": str(pnl), "reason": reason},
        )

        remaining = position.quantity - quantity
        if remaining > 0:
            self._positions[symbol] = Position(
                symbol=position.symbol,
                side=position.side,
                quantity=remaining,
                entry_price=position.entry_price,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                opened_at=position.opened_at,
                broker_position_id=position.broker_position_id,
            )
        else:
            del self._positions[symbol]
            self._excursions.forget(symbol)

    def _close_position(self, symbol: str, price: Decimal, *, reason: str) -> None:
        position = self._positions.get(symbol)
        if position is None:
            return
        self._reduce_position(symbol, position.quantity, price, reason=reason)

    # ------------------------------------------------------ 内部: 評価計算

    def _series(self, symbol: str) -> list[Candle]:
        series = self._candles.get(symbol)
        if not series:
            raise BrokerError(f"{symbol} の価格データがありません")
        return series

    def _current_candle(self, symbol: str) -> Candle:
        series = self._series(symbol)
        index = min(self._cursor[symbol], len(series)) - 1
        return series[max(0, index)]

    def _unrealized(self, position: Position) -> Decimal:
        price = self._current_candle(position.symbol).close
        return position.pnl_at(price) * self._contract_size

    def _margin_for(self, position: Position) -> Decimal:
        price = self._current_candle(position.symbol).close
        return self._required_margin(price, position.quantity)

    def _required_margin(self, price: Decimal, quantity: Decimal) -> Decimal:
        return price * quantity * self._contract_size / self._leverage

    def _available_margin(self) -> Decimal:
        unrealized = sum((self._unrealized(p) for p in self._positions.values()), Decimal(0))
        used = sum((self._margin_for(p) for p in self._positions.values()), Decimal(0))
        return max(Decimal(0), self._cash + unrealized - used)

    def _find_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is not None:
            return order
        for candidate in self._orders.values():
            if candidate.broker_order_id == order_id:
                return candidate
        raise BrokerError(f"注文が見つかりません: {order_id}")

    # ------------------------------------------------------------ 補助

    @property
    def realized_pnl(self) -> Decimal:
        """開始からの確定損益合計。"""
        return self._cash - self._initial_balance

    def inject_candles(self, symbol: str, candles: list[Candle]) -> None:
        """価格系列を差し替える（テスト用）。"""
        if not candles:
            raise ValueError("candles が空です")
        self._candles[symbol] = candles
        self._cursor.setdefault(symbol, 0)
        self._cursor[symbol] = min(self._cursor[symbol], len(candles))
        if symbol not in self._symbols:
            self._symbols.append(symbol)

    @staticmethod
    def _d(value: float | int | str | Decimal) -> Decimal:
        return to_decimal(value)
