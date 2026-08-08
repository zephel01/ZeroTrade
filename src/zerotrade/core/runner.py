"""StrategyRunner — 運用フローの実行ループ。

docs/plan.md の運用フローをそのままコードにしたもの:

1. 戦略がシグナルを生成
2. RiskManager がリスクチェック
3. 通過した場合のみ OrderManager 経由で発注
4. 約定・ポジション状態を監視
5. 利確・損切り・強制決済をルールに従って実行
6. 日次でサマリ通知

1ループ（:meth:`step`）は完全に非同期。例外で全体が落ちないよう、
銘柄単位の失敗は握りつぶしてログへ流し、次の銘柄へ進む。
ただし設定不備やリスク状態の破損など「続けてはいけない」失敗は送出する。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from zerotrade.brokers.base import BaseBroker
from zerotrade.control import KillSwitch
from zerotrade.core.excursion import ExcursionTracker
from zerotrade.core.notifier import Notifier, NullNotifier
from zerotrade.core.orders import OrderManager
from zerotrade.core.risk import MarketContext, RiskManager
from zerotrade.core.sizing import PositionSizer
from zerotrade.data.feed import MarketDataFeed
from zerotrade.errors import BrokerError
from zerotrade.log import get_logger
from zerotrade.models import (
    Balance,
    Candle,
    ClosedTrade,
    Order,
    OrderRequest,
    Position,
    Side,
    Signal,
    SignalAction,
    Ticker,
)
from zerotrade.settings import Settings
from zerotrade.store import Store
from zerotrade.strategies.base import Strategy, StrategyContext
from zerotrade.strategies.indicators import atr

__all__ = ["RunnerStats", "StrategyRunner"]

logger = get_logger(__name__)


@dataclass
class RunnerStats:
    """実行ループの累積カウンタ。動作確認とテストの検証に使う。"""

    iterations: int = 0
    signals: int = 0
    entries: int = 0
    exits: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    stop_updates: int = 0
    """トレーリングストップを引き上げた回数。"""

    def reject(self, rule: str) -> None:
        self.rejections[rule] = self.rejections.get(rule, 0) + 1


class StrategyRunner:
    """戦略・リスク・発注をつなぐ実行ループ。"""

    def __init__(
        self,
        *,
        settings: Settings,
        broker: BaseBroker,
        feed: MarketDataFeed,
        strategy: Strategy,
        risk: RiskManager,
        sizer: PositionSizer,
        orders: OrderManager | None = None,
        notifier: Notifier | None = None,
        store: Store | None = None,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._feed = feed
        self._strategy = strategy
        self._risk = risk
        self._sizer = sizer
        self._orders = orders or OrderManager(broker, risk)
        self._notifier = notifier or NullNotifier()
        self._store = store
        self._kill_switch = KillSwitch(settings.state_dir)

        self._stop_event = asyncio.Event()
        self._last_equity_record: datetime | None = None
        self._previous_positions: dict[str, Position] = {}
        self._seen_trades: set[str] = set()
        self._last_day_key = risk.state.day_key
        self.stats = RunnerStats()

        # ライブでは足の高値安値が取れないので、ループごとの気配値を標本にする。
        # 取りこぼす方向にしか外れないので、判断材料としては安全側。
        self._excursions = ExcursionTracker(settings.sizing.contract_size)

        # ATR の「平常時」基準に使う長期期間。設定の atr_period の5倍を既定とする。
        self._atr_period = int(settings.strategy.params.get("atr_period", 14))
        self._baseline_period = self._atr_period * 5

    # ------------------------------------------------------------ 制御

    @property
    def orders(self) -> OrderManager:
        return self._orders

    def stop(self) -> None:
        """次のループ境界で停止する。"""
        self._stop_event.set()

    async def run(self, *, max_iterations: int | None = None) -> RunnerStats:
        """実行ループを開始する。

        Args:
            max_iterations: 指定回数だけ回して停止する（テスト・検証用）。
                ``None`` なら :meth:`stop` が呼ばれるまで回り続ける。
        """
        # 前回の停止要求が残っていると起動直後に止まってしまう。
        # 起動そのものが明示的な再開の意思表示なので、ここで消す。
        if (stale := self._kill_switch.requested()) is not None:
            logger.warning("前回の緊急停止要求を解除しました（%s）", stale)
            self._kill_switch.clear()

        await self._broker.connect()
        try:
            balance = await self._broker.get_balance()
            self._risk.set_reference_equity(balance.equity)
            await self._notifier.send(
                f"ZeroTrade を開始しました（mode={self._settings.mode} / "
                f"broker={self._broker.name} / equity={balance.equity:.2f} "
                f"{balance.currency}）"
            )
            self._record_event(
                "start",
                f"mode={self._settings.mode} broker={self._broker.name} "
                f"strategy={self._strategy.name} equity={balance.equity}",
            )
            await self._orders.sync_open_orders()

            while not self._stop_event.is_set():
                if await self._check_kill_switch():
                    break
                await self.step()
                if max_iterations is not None and self.stats.iterations >= max_iterations:
                    break
                if self._stop_event.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._settings.poll_interval_seconds
                    )
        finally:
            await self._shutdown()
        return self.stats

    async def _check_kill_switch(self) -> bool:
        """ダッシュボードなど外部からの緊急停止要求を確認する。

        Returns:
            停止すべきか。
        """
        reason = self._kill_switch.requested()
        if reason is None:
            return False
        logger.warning("緊急停止が要求されました: %s", reason)
        self._record_event("kill_switch", reason)
        await self._notifier.send(f"⏹ 緊急停止が要求されました: {reason}", level="warning")
        self._stop_event.set()
        return True

    async def _shutdown(self) -> None:
        """停止時のクリーンアップ。建玉は閉じない（意図的）。

        自動的に全決済すると、一時的なネットワーク断で再起動しただけで
        意図しない損失確定が起きる。建玉の扱いは運用者の判断に委ねる。
        """
        with contextlib.suppress(BrokerError):
            await self._orders.cancel_all()
        await self._notifier.send(f"ZeroTrade を停止しました。{self._risk.summary()}")
        self._record_event("stop", self._risk.summary())
        self._risk.save()
        await self._broker.disconnect()

    # ------------------------------------------------------------ 1ループ

    async def step(self) -> None:
        """ループ1回ぶんの処理。"""
        self.stats.iterations += 1

        await self._orders.refresh()
        await self._sync_closed_trades()

        balance = await self._broker.get_balance()
        positions = {p.symbol: p for p in await self._broker.get_positions()}
        await self._infer_closed_trades(positions)
        self._maybe_record_equity(balance, len(positions))

        for symbol in self._settings.symbols:
            try:
                await self._process_symbol(symbol, balance, positions)
            except BrokerError as exc:
                # 1銘柄の通信失敗で全体を止めない。
                self.stats.errors += 1
                logger.warning("%s の処理に失敗しました: %s", symbol, exc)

        await self._maybe_daily_summary()

    async def _process_symbol(
        self, symbol: str, balance: Balance, positions: dict[str, Position]
    ) -> None:
        # 足種は設定から取る。既定のまま（M5）で H1 用の戦略を回すと、
        # バックテストとまったく別物になる。
        candles = await self._feed.get_candles(
            symbol,
            granularity=self._settings.strategy.granularity,
            count=max(self._strategy.warmup_bars, self._baseline_period) + 10,
        )
        if not candles:
            logger.debug("%s の足が取得できませんでした", symbol)
            return

        ticker = await self._feed.get_ticker(symbol)
        position = positions.get(symbol)

        if position is not None:
            # 決済されたときに MFE/MAE として残す。決済判定より前に測る。
            self._excursions.observe_price(position, ticker.mid)

        # --- 保険としての強制決済 -------------------------------------------
        # ブローカー側のストップが機能していない場合の最後の砦。
        if position is not None and await self._force_exit_if_needed(
            position, ticker, balance, positions
        ):
            return

        signal = self._strategy.generate(
            StrategyContext(symbol=symbol, candles=candles, ticker=ticker, position=position)
        )
        self.stats.signals += 1

        if signal.action is SignalAction.HOLD:
            # HOLD は毎ループ出るため記録しない（残すとテーブルが埋まるだけになる）。
            logger.debug("%s: HOLD（%s）", symbol, signal.reason)
            return

        if self._store is not None and self._settings.store.record_signals:
            self._store.record_signal(signal)

        if signal.action is SignalAction.UPDATE_STOP:
            await self._handle_stop_update(signal, position)
            return

        if signal.action is SignalAction.EXIT:
            await self._handle_exit(signal, position, ticker, balance, positions)
            return

        await self._handle_entry(
            signal,
            candles,
            ticker,
            balance,
            positions,
        )

    # ------------------------------------------------------------ 決済

    async def _handle_exit(
        self,
        signal: Signal,
        position: Position | None,
        ticker: Ticker,
        balance: Balance,
        positions: dict[str, Position],
    ) -> None:
        if position is None:
            return
        result = await self._orders.close_position(
            position,
            balance=balance,
            positions=positions.values(),
            reference_price=ticker.price_for(position.side.opposite),
        )
        if result.submitted:
            self.stats.exits += 1
            self._record_order(result.order)
            await self._notifier.send(
                f"決済: {position.symbol} {position.side} {position.quantity}（{signal.reason}）"
            )

    async def _handle_stop_update(self, signal: Signal, position: Position | None) -> None:
        """トレーリングストップの引き上げを反映する。

        ブローカーが対応していなければ黙って無視する。順張り戦略の
        利益は伸びなくなるが、取引そのものは続けられる。
        """
        if position is None or signal.stop_loss is None:
            return
        try:
            updated = await self._broker.update_position_stop(signal.symbol, signal.stop_loss)
        except BrokerError as exc:
            logger.debug("ストップ更新に対応していません: %s", exc)
            return
        if updated is not None:
            self.stats.stop_updates += 1
            logger.debug(
                "%s のストップを %s へ更新しました（%s）",
                signal.symbol,
                updated.stop_loss,
                signal.reason,
            )

    async def _force_exit_if_needed(
        self,
        position: Position,
        ticker: Ticker,
        balance: Balance,
        positions: dict[str, Position],
    ) -> bool:
        """ストップ/利確価格を明確に超えていれば強制決済する。

        Returns:
            決済を実行したか。
        """
        price = ticker.mid
        breached: str | None = None
        if position.side is Side.BUY:
            if position.stop_loss is not None and price <= position.stop_loss:
                breached = "ストップ"
            elif position.take_profit is not None and price >= position.take_profit:
                breached = "利確"
        else:
            if position.stop_loss is not None and price >= position.stop_loss:
                breached = "ストップ"
            elif position.take_profit is not None and price <= position.take_profit:
                breached = "利確"

        if breached is None:
            return False

        # close_position がブローカーへ建玉を確認し直す。既にストップで
        # 約定していれば送信されずに戻るので、その場合は何も起きていない。
        # 警告と通知は、実際に強制決済したときだけ出す。
        result = await self._orders.close_position(
            position,
            balance=balance,
            positions=positions.values(),
            reference_price=ticker.price_for(position.side.opposite),
        )
        if not result.submitted:
            logger.debug(
                "%s: %s価格を超過していたが、建玉は既に決済済みだった",
                position.symbol,
                breached,
            )
            return False

        logger.warning(
            "%s: %s価格を超過したため強制決済しました（現在値 %s）",
            position.symbol,
            breached,
            price,
        )
        self.stats.exits += 1
        self._record_order(result.order)
        self._record_event("force_exit", f"{position.symbol} {breached} 超過（現在値 {price}）")
        await self._notifier.send(
            f"強制決済（{breached}）: {position.symbol} {position.side} {position.quantity}",
            level="warning",
        )
        return True

    # ------------------------------------------------------------ 新規

    async def _handle_entry(
        self,
        signal: Signal,
        candles: list[Candle],
        ticker: Ticker,
        balance: Balance,
        positions: dict[str, Position],
    ) -> None:
        side = signal.side
        if side is None:
            return

        entry_price = ticker.price_for(side)
        sizing = self._sizer.calculate(
            equity=balance.equity,
            entry_price=entry_price,
            stop_loss=signal.stop_loss,
            max_quantity=self._max_quantity_by_margin(balance, entry_price),
        )
        if not sizing:
            logger.info("%s: サイズ0のため見送り（%s）", signal.symbol, sizing.reason)
            self.stats.reject("zero_size")
            if self._store is not None:
                # 実際の OrderRequest は作られていないので、意図した内容で1件残す。
                self._store.record_rejection(
                    OrderRequest(
                        symbol=signal.symbol,
                        side=side,
                        quantity=Decimal(1),
                        stop_loss=signal.stop_loss,
                    ),
                    "zero_size",
                    sizing.reason,
                )
            return

        request = OrderRequest(
            symbol=signal.symbol,
            side=side,
            quantity=sizing.quantity,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            # 発注を決めた時点の価格。実際の約定との差が滑りの実測値になる。
            reference_price=entry_price,
            metadata={"strategy": signal.strategy, "reason": signal.reason},
        )

        result = await self._orders.submit(
            request,
            balance=balance,
            positions=positions.values(),
            market=self._market_context(candles, ticker),
        )

        if result.submitted:
            self.stats.entries += 1
            self._record_order(result.order)
            await self._notifier.send(
                f"新規: {signal.symbol} {side} {sizing.quantity} @ {entry_price} "
                f"/ SL {signal.stop_loss} / TP {signal.take_profit}"
                f"（想定リスク {sizing.risk_amount:.0f} {balance.currency}・{signal.reason}）"
            )
        elif result.decision.rule:
            self.stats.reject(result.decision.rule)
            if self._store is not None:
                self._store.record_rejection(request, result.decision.rule, result.decision.detail)

    def _max_quantity_by_margin(self, balance: Balance, price: Decimal) -> Decimal | None:
        """証拠金使用率の上限から逆算した最大数量。

        サイズ計算の段階で丸めておくと、
        RiskManager の ``max_margin_usage`` で毎回全却下されるのを防げる。
        """
        risk = self._risk.settings
        allowed_margin = balance.equity * risk.max_margin_usage - balance.used_margin
        allowed_margin = min(allowed_margin, balance.available)
        if allowed_margin <= 0:
            return Decimal(0)
        notional = allowed_margin * risk.assumed_leverage
        denominator = price * self._settings.sizing.contract_size
        if denominator <= 0:
            return None
        return notional / denominator

    def _market_context(self, candles: list[Candle], ticker: Ticker) -> MarketContext:
        """気配値・ATR・平常時ATR をまとめてリスク判定へ渡す。

        ticker を必ず入れること。成行注文はこれが唯一の参照価格であり、
        欠けると RiskManager が ``no_reference_price`` で全却下する。
        """
        return MarketContext(
            ticker=ticker,
            atr=atr(candles, self._atr_period),
            atr_baseline=atr(candles, self._baseline_period),
        )

    # ------------------------------------------------------------ 損益反映

    async def _sync_closed_trades(self) -> None:
        """新しく決済されたトレードを RiskManager へ反映する。

        含み損ではなく確定損益だけを日次・週次カウンタへ入れる。
        ブローカーが決済履歴を返せない場合は建玉の差分から推定する
        （:meth:`_infer_closed_trades` を参照）。ここを素通りさせると
        **日次・週次の損失上限が一切働かなくなる**。
        """
        if not self._broker.supports_closed_trades:
            return

        try:
            trades = await self._broker.get_closed_trades()
        except BrokerError as exc:
            logger.warning("決済履歴の取得に失敗しました: %s", exc)
            return

        for trade in trades:
            key = trade.trade_id or f"{trade.symbol}:{trade.closed_at.isoformat()}"
            key = f"{key}:{trade.closed_at.isoformat()}:{trade.realized_pnl}"
            if key in self._seen_trades:
                continue
            self._seen_trades.add(key)
            self._risk.record_trade_closed(trade.symbol, trade.realized_pnl)
            if self._store is not None:
                self._store.record_trade(self._with_excursion(trade))

            if self._risk.is_halted:
                self._record_event("halt", self._risk.summary())
                await self._notifier.send(
                    f"⚠ 損失上限に達したため取引を停止しました。{self._risk.summary()}",
                    level="error",
                )

    async def _infer_closed_trades(self, positions: dict[str, Position]) -> None:
        """建玉の差分から確定損益を推定する。

        決済履歴を返せないブローカー（多くの取引所、ccxt 経由を含む）向けの
        代替経路。これが無いと :class:`RiskManager` は確定損益を一切知らず、
        **日次・週次の損失上限が働かない**。安全側の欠落なので必ず塞ぐ。

        推定であることの限界も書いておく。決済価格は検知時点の気配値で
        代用するため、前回のループから今回までの間に動いた分だけ誤差が出る。
        正確な値が必要なら :meth:`BaseBroker.get_closed_trades` を実装して
        :attr:`supports_closed_trades` を True にすること。
        """
        if self._broker.supports_closed_trades:
            # 正確な履歴が取れるなら推定は不要（二重計上になる）。
            self._previous_positions = dict(positions)
            return

        previous = self._previous_positions
        self._previous_positions = dict(positions)
        if not previous:
            return

        for symbol, before in previous.items():
            after = positions.get(symbol)
            if after is not None and after.side is before.side:
                closed = before.quantity - after.quantity
            else:
                # 建玉が消えた、または反対方向へ入れ替わった。
                closed = before.quantity
            if closed <= 0:
                continue

            try:
                exit_price = (await self._feed.get_ticker(symbol)).price_for(before.side.opposite)
            except BrokerError as exc:
                logger.warning("%s の決済価格を取得できませんでした: %s", symbol, exc)
                continue

            pnl = (
                (exit_price - before.entry_price)
                * before.side.sign
                * closed
                * self._settings.sizing.contract_size
            )
            logger.info(
                "建玉の差分から決済を検知しました: %s %s %s（推定損益 %+.2f）",
                symbol,
                before.side,
                closed,
                pnl,
            )
            self._risk.record_trade_closed(symbol, pnl)
            if self._store is not None:
                ratio = closed / before.quantity if before.quantity > 0 else Decimal(1)
                excursion = self._excursions.snapshot(symbol, ratio=ratio)
                self._store.record_trade(
                    ClosedTrade(
                        symbol=symbol,
                        side=before.side,
                        quantity=closed,
                        entry_price=before.entry_price,
                        exit_price=exit_price,
                        realized_pnl=pnl,
                        opened_at=before.opened_at,
                        trade_id=before.broker_position_id or symbol,
                        reason="inferred",
                        mfe=None if excursion is None else excursion.favorable,
                        mae=None if excursion is None else excursion.adverse,
                    )
                )
            if after is None:
                self._excursions.forget(symbol)
            if self._risk.is_halted:
                self._record_event("halt", self._risk.summary())
                await self._notifier.send(
                    f"⚠ 損失上限に達したため取引を停止しました。{self._risk.summary()}",
                    level="error",
                )

    async def _maybe_daily_summary(self) -> None:
        """日付が変わったらサマリを通知する。"""
        current = self._risk.state.day_key
        if current == self._last_day_key:
            return
        await self._notifier.send(f"日次サマリ: {self._risk.summary()}")
        self._record_event("daily_summary", self._risk.summary())
        self._last_day_key = current
        # 新しい日の損失上限は、その日の開始 equity を基準にする。
        with contextlib.suppress(BrokerError):
            balance = await self._broker.get_balance()
            self._risk.set_reference_equity(balance.equity)

    # ------------------------------------------------------------ 記録

    def _with_excursion(self, trade: ClosedTrade) -> ClosedTrade:
        """ブローカーが返したトレードに MFE/MAE を補う。

        取引所は建玉中の含み損益の履歴を返さないので、こちらが
        ループごとに観測していた値を貼る。既に入っていれば触らない。
        """
        if trade.mfe is not None or trade.mae is not None:
            return trade
        excursion = self._excursions.snapshot(trade.symbol)
        if excursion is None:
            return trade
        self._excursions.forget(trade.symbol)
        return replace(trade, mfe=excursion.favorable, mae=excursion.adverse)

    def _record_order(self, order: Order | None) -> None:
        if self._store is not None and order is not None:
            self._store.record_order(order)

    def _record_event(self, kind: str, detail: str) -> None:
        if self._store is not None:
            self._store.record_event(kind, detail)

    def _maybe_record_equity(self, balance: Balance, open_positions: int) -> None:
        """equity のスナップショットを一定間隔で残す。

        毎ループ書くと数万行に膨れてグラフが重くなるだけなので、
        ``store.equity_interval_seconds`` ごとに間引く。
        """
        if self._store is None:
            return
        now = balance.timestamp
        interval = self._settings.store.equity_interval_seconds
        if (
            self._last_equity_record is not None
            and (now - self._last_equity_record).total_seconds() < interval
        ):
            return
        self._last_equity_record = now
        self._store.record_equity(balance, open_positions=open_positions)
