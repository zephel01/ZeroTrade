"""OrderManager — 発注の唯一の入口。

戦略コードがブローカーを直接触れないようにするための層。
``submit()`` は必ず :class:`~zerotrade.core.risk.RiskManager` を通し、
承認された場合にのみブローカーへ委譲する。

追跡している内容:

* クライアント注文ID ↔ ブローカー注文ID の対応
* 未約定注文の現在状態（部分約定を含む）
* 却下された注文とその理由
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import Decimal

from zerotrade.brokers.base import BaseBroker
from zerotrade.core.risk import MarketContext, RiskDecision, RiskManager
from zerotrade.errors import BrokerError
from zerotrade.log import get_logger
from zerotrade.models import Balance, Order, OrderRequest, OrderStatus, Position

__all__ = ["OrderManager", "SubmitResult"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """発注の結果。

    ``order`` が ``None`` の場合、注文は送信されていない。
    理由は ``decision`` （リスク却下）か ``error`` （ブローカー障害）を見る。
    """

    decision: RiskDecision
    order: Order | None = None
    error: str | None = None

    @property
    def submitted(self) -> bool:
        return self.order is not None

    def __bool__(self) -> bool:
        return self.submitted


class OrderManager:
    """注文のライフサイクル管理。"""

    def __init__(self, broker: BaseBroker, risk: RiskManager) -> None:
        self._broker = broker
        self._risk = risk
        self._orders: dict[str, Order] = {}
        self._by_broker_id: dict[str, str] = {}

    # ------------------------------------------------------------- 参照系

    @property
    def orders(self) -> dict[str, Order]:
        """クライアント注文ID → 注文（追跡中のすべて）。"""
        return dict(self._orders)

    def active_orders(self, symbol: str | None = None) -> list[Order]:
        """まだ約定しうる注文の一覧。"""
        return [
            order
            for order in self._orders.values()
            if order.is_active and (symbol is None or order.symbol == symbol)
        ]

    def get(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    # ------------------------------------------------------------- 発注

    async def submit(
        self,
        request: OrderRequest,
        *,
        balance: Balance,
        positions: Iterable[Position],
        market: MarketContext | None = None,
    ) -> SubmitResult:
        """リスク検査を通してから発注する。

        リスク違反でも例外は投げず :class:`SubmitResult` を返す。
        自動売買ループが1件の却下で止まらないようにするため。
        例外で止めたい場合は ``result.decision.raise_if_rejected()`` を呼ぶ。
        """
        decision = self._risk.evaluate(request, balance=balance, positions=positions, market=market)
        if not decision:
            logger.info(
                "注文を却下しました: %s %s %s（%s）",
                request.symbol,
                request.side,
                request.quantity,
                decision.detail,
                extra={"rule": decision.rule, "client_order_id": request.client_order_id},
            )
            return SubmitResult(decision=decision)

        try:
            order = await self._broker.place_order(request)
        except BrokerError as exc:
            logger.error(
                "発注に失敗しました: %s（%s）",
                request.symbol,
                exc,
                extra={"client_order_id": request.client_order_id},
            )
            return SubmitResult(decision=decision, error=str(exc))

        # 滑りの実測に使う想定価格。ブローカーが埋めていなければ
        # 発注を決めた時点の価格をここで引き継ぐ。_track より前にやること。
        if order.reference_price is None:
            order.reference_price = request.reference_price

        self._track(order)
        self._risk.record_order_submitted(request)
        logger.info(
            "発注しました: %s %s %s @ %s",
            order.symbol,
            order.side,
            order.quantity,
            order.average_price or order.limit_price or "market",
            extra={"client_order_id": order.client_order_id, "status": order.status},
        )
        return SubmitResult(decision=decision, order=order)

    # ------------------------------------------------------------- 取消・更新

    async def cancel(self, client_order_id: str) -> Order | None:
        """追跡中の注文を取り消す。"""
        order = self._orders.get(client_order_id)
        if order is None or not order.is_active:
            return order
        target_id = order.broker_order_id or client_order_id
        try:
            updated = await self._broker.cancel_order(target_id)
        except BrokerError as exc:
            logger.warning("注文取消に失敗しました: %s（%s）", client_order_id, exc)
            return order
        self._track(updated)
        return updated

    async def cancel_all(self, symbol: str | None = None) -> list[Order]:
        """未約定注文をすべて取り消す。停止時のクリーンアップに使う。"""
        results: list[Order] = []
        for order in self.active_orders(symbol):
            updated = await self.cancel(order.client_order_id)
            if updated is not None:
                results.append(updated)
        return results

    async def refresh(self) -> list[Order]:
        """未約定注文の状態をブローカーへ問い合わせて更新する。

        Returns:
            この呼び出しで終了状態（約定・取消など）へ遷移した注文。
        """
        finished: list[Order] = []
        for order in self.active_orders():
            target_id = order.broker_order_id or order.client_order_id
            try:
                updated = await self._broker.get_order(target_id)
            except BrokerError as exc:
                logger.warning("注文状態の取得に失敗しました: %s（%s）", target_id, exc)
                continue
            previous = order.status
            self._track(updated)
            if updated.status.is_terminal and not previous.is_terminal:
                finished.append(updated)
        return finished

    async def sync_open_orders(self, symbol: str | None = None) -> list[Order]:
        """ブローカー側の未約定注文を取り込む。

        再起動後に「システムは知らないがブローカーには残っている注文」を
        取りこぼさないための同期処理。
        """
        orders = await self._broker.get_open_orders(symbol)
        for order in orders:
            self._track(order)
        return orders

    # ------------------------------------------------------------- 決済

    async def close_position(
        self,
        position: Position,
        *,
        balance: Balance,
        positions: Iterable[Position],
        reference_price: Decimal | None = None,
    ) -> SubmitResult:
        """建玉を成行で決済する。``reduce_only`` なのでリスク検査は素通しになる。

        送信の直前にブローカーへ建玉を問い合わせ直す。呼び出し側が持っている
        建玉の情報は1ループぶん古いことがあり、その間にストップが約定していると
        「決済のつもりの注文」がそのまま新規建てになってしまう。
        決済は頻度が低いので、この確認1回ぶんのコストは払う価値がある。
        """
        try:
            current = await self._broker.get_positions()
        except BrokerError as exc:
            logger.warning("建玉の確認に失敗したため決済を見送ります: %s", exc)
            return SubmitResult(decision=RiskDecision.approve(), error=str(exc))

        live = next(
            (p for p in current if p.symbol == position.symbol and p.side is position.side),
            None,
        )
        if live is None:
            logger.info("%s の建玉は既に決済済みでした（%s）", position.symbol, position.side)
            return SubmitResult(decision=RiskDecision.approve(), error="建玉が既に存在しません")

        request = OrderRequest(
            symbol=live.symbol,
            side=live.side.opposite,
            # 実際に残っている数量で出す。多すぎると余剰が新規建てになる。
            quantity=min(position.quantity, live.quantity),
            reduce_only=True,
            reference_price=reference_price,
            metadata={"close_of": live.broker_position_id or live.symbol},
        )
        return await self.submit(request, balance=balance, positions=positions)

    # ------------------------------------------------------------- 内部

    def _track(self, order: Order) -> None:
        """注文の最新状態を保持する。終了した注文も履歴として残す。

        ブローカーが内部で保持しているインスタンスをそのまま抱えると、
        向こう側の破壊的更新が手元の「前回の状態」まで書き換えてしまい、
        :meth:`refresh` が状態遷移を検知できなくなる。必ず複製して持つ。
        """
        snapshot = replace(order, metadata=dict(order.metadata))
        self._orders[order.client_order_id] = snapshot
        if snapshot.broker_order_id:
            self._by_broker_id[snapshot.broker_order_id] = snapshot.client_order_id

    def resolve_client_id(self, broker_order_id: str) -> str | None:
        """ブローカー注文IDからクライアント注文IDを引く。"""
        return self._by_broker_id.get(broker_order_id)

    def filled_quantity(self, symbol: str) -> Decimal:
        """その銘柄で約定済みの数量合計（買い正 / 売り負）。"""
        total = Decimal(0)
        for order in self._orders.values():
            if order.symbol != symbol:
                continue
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                total += order.filled_quantity * order.side.sign
        return total
