"""発注経路の検証（配管テスト）。

**このモジュールだけは、意図的に本物の注文を出す。**

## なぜ必要か

ZeroTrade の発注経路は、モックとペーパーブローカーでしか通していない。
テストは通るが、それは「自分が書いた偽物が期待どおりに応答する」ことを
確かめているにすぎない。取引所が実際に何を受け取り、何を返すかは別の話である。

過去に実際起きた食い違いを挙げる。いずれもモックでは見つからなかった。

* ``reduce_only`` を無視する経路があり、決済注文が**新規建てとして通った**
* ``fetchPositions`` に銘柄リストを渡さないと建玉が取れず、二重に建てる
* 永続契約の統一シンボルは ``BTC/USDT`` ではなく ``BTC/USDT:USDT``
* 足の取得上限が取引所ごとに違い、超えると**1本も返らない**

**実物に触れないと分からないことがある。** その一点を、最小の金額で潰す。

## 何をするか

最小数量で1往復させ、経路上の確認項目を1つずつ検査して合否を出す。
BTC/USDT なら 0.0001 枚（数ドル相当）で、往復コストは数セントに収まる。

途中で失敗しても、**建玉を残したまま終わらない**。最後に必ず決済を試み、
残ってしまった場合は画面とログの両方で警告する。

## 使い方

```bash
zerotrade -c config/verify.yaml verify --symbol BTC_USDT
```

``--dry-run`` を付けると発注せず、読み取り系の確認だけを行う。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from zerotrade.brokers import create_broker
from zerotrade.brokers.base import BaseBroker
from zerotrade.errors import BrokerError, ZeroTradeError
from zerotrade.log import get_logger
from zerotrade.models import OrderRequest, OrderStatus, Position, Side
from zerotrade.settings import Settings

__all__ = ["CheckResult", "VerifyReport", "run_verification"]

logger = get_logger(__name__)

#: 建玉が反映されるまでの待ち時間と再試行回数。
SETTLE_WAIT_SECONDS = 2.0
SETTLE_RETRIES = 5


@dataclass(slots=True)
class CheckResult:
    """確認項目1つぶんの結果。"""

    name: str
    passed: bool
    detail: str = ""
    critical: bool = True
    """False なら、失敗しても全体の合否を左右しない（環境差など）。"""

    def line(self) -> str:
        mark = "OK  " if self.passed else ("NG  " if self.critical else "警告")
        return f"  [{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass(slots=True)
class VerifyReport:
    """配管テスト全体の結果。"""

    checks: list[CheckResult] = field(default_factory=list)
    position_left_open: bool = False

    def add(self, name: str, passed: bool, detail: str = "", *, critical: bool = True) -> None:
        result = CheckResult(name=name, passed=passed, detail=detail, critical=critical)
        self.checks.append(result)
        logger.info("%s", result.line().strip())

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.critical]

    @property
    def ok(self) -> bool:
        return not self.failures and not self.position_left_open


async def _current_position(broker: BaseBroker, symbol: str) -> Position | None:
    positions = [p for p in await broker.get_positions() if p.symbol == symbol]
    return positions[0] if positions else None


async def _wait_until_open(broker: BaseBroker, symbol: str) -> Position | None:
    """建玉が現れるのを待つ。取引所側の反映は即時とは限らない。"""
    for _ in range(SETTLE_RETRIES):
        position = await _current_position(broker, symbol)
        if position is not None:
            return position
        await asyncio.sleep(SETTLE_WAIT_SECONDS)
    return None


async def _wait_until_flat(broker: BaseBroker, symbol: str) -> bool:
    """建玉が消えるのを待つ。**消えたかどうかを bool で返す。**

    「建玉なし」と「確認できなかった」を同じ ``None`` で返すと、
    決済できていないのに合格と報告してしまう。ここは取り違えると
    建玉を置き去りにするので、真偽値で明示する。
    """
    for _ in range(SETTLE_RETRIES):
        if await _current_position(broker, symbol) is None:
            return True
        await asyncio.sleep(SETTLE_WAIT_SECONDS)
    return False


async def run_verification(
    settings: Settings,
    symbol: str,
    *,
    quantity: Decimal | None = None,
    dry_run: bool = False,
) -> VerifyReport:
    """発注経路を検証する。``dry_run`` でなければ**本物の注文を出す**。"""
    report = VerifyReport()
    broker = create_broker(settings)

    try:
        await broker.connect()
        report.add("接続", True, broker.name)
    except ZeroTradeError as exc:
        report.add("接続", False, str(exc))
        return report

    try:
        await _read_only_checks(broker, symbol, report)
        if dry_run:
            report.add("発注検証", True, "--dry-run のため省略", critical=False)
            return report
        await _round_trip(broker, symbol, quantity, report)
    finally:
        await _ensure_flat(broker, symbol, report)
        await broker.disconnect()

    return report


async def _read_only_checks(broker: BaseBroker, symbol: str, report: VerifyReport) -> None:
    """発注せずに確かめられること。"""
    try:
        balance = await broker.get_balance()
        report.add(
            "残高照会",
            balance.equity > 0,
            f"{balance.equity} {balance.currency}"
            + ("" if balance.equity > 0 else "（残高がゼロでは発注できません）"),
        )
    except ZeroTradeError as exc:
        report.add("残高照会", False, str(exc))

    try:
        ticker = await broker.get_ticker(symbol)
        spread = ticker.ask - ticker.bid
        relative = (spread / ticker.mid * 10000) if ticker.mid > 0 else Decimal(0)
        report.add("気配値取得", ticker.bid > 0 < ticker.ask, f"スプレッド {relative:.2f}bp")
    except ZeroTradeError as exc:
        report.add("気配値取得", False, str(exc))

    try:
        positions = await broker.get_positions()
        left = [p for p in positions if p.symbol == symbol]
        report.add(
            "建玉照会",
            not left,
            "既に建玉があります。検証前に手仕舞ってください" if left else "建玉なし",
        )
    except ZeroTradeError as exc:
        report.add("建玉照会", False, str(exc))

    try:
        candles = await broker.get_ohlcv(symbol, granularity="H1", count=50)
        report.add("足の取得", len(candles) > 10, f"{len(candles)}本")
        incomplete = [c for c in candles if not c.complete]
        report.add(
            "未確定足の判別",
            True,
            f"未確定 {len(incomplete)}本を識別",
            critical=False,
        )
    except ZeroTradeError as exc:
        report.add("足の取得", False, str(exc))


async def _round_trip(
    broker: BaseBroker, symbol: str, quantity: Decimal | None, report: VerifyReport
) -> None:
    """最小数量で新規→決済を一往復させる。"""
    ticker = await broker.get_ticker(symbol)
    size = quantity if quantity is not None else Decimal("0.0001")

    # ストップは十分遠くに置く。検証中に引っかかると経路の確認にならない。
    stop = (ticker.bid * Decimal("0.80")).quantize(Decimal("0.00000001"))
    notional = size * ticker.ask
    logger.info("最小数量で発注します: %s %s（想定元本 %.4f）", symbol, size, notional)

    try:
        order = await broker.place_order(
            OrderRequest(symbol=symbol, side=Side.BUY, quantity=size, stop_loss=stop)
        )
        report.add(
            "新規注文",
            order.status is not OrderStatus.REJECTED,
            f"{order.status.value} / 取引所ID {order.broker_order_id}",
        )
        report.add(
            "冪等キーの往復",
            bool(order.client_order_id),
            f"clientOrderId {order.client_order_id}",
            critical=False,
        )
    except ZeroTradeError as exc:
        report.add("新規注文", False, str(exc))
        return

    position = await _wait_until_open(broker, symbol)
    report.add(
        "建玉の反映",
        position is not None,
        f"数量 {getattr(position, 'quantity', '-')}" if position else "建玉が現れませんでした",
    )
    if position is None:
        return

    # ストップが取引所側に入ったか。**ここが入っていない建玉は無防備**で、
    # プロセスが落ちれば誰も見ていない状態になる。
    await _check_stop_attached(broker, symbol, position, report)

    # 決済注文が「反対側の新規建て」にならないことの確認。
    # ここが本番で壊れると、建玉が減るどころか増える。
    try:
        closed = await broker.close_position(symbol)
        report.add("決済注文", closed is not None, "reduce_only で送信")
    except ZeroTradeError as exc:
        report.add("決済注文", False, str(exc))
        return

    flat = await _wait_until_flat(broker, symbol)
    report.add(
        "建玉の解消",
        flat,
        "決済で建玉がゼロになった" if flat else "建玉が残っています（反対側に建った可能性）",
    )

    if broker.supports_closed_trades:
        try:
            trades = await broker.get_closed_trades()
            report.add(
                "確定損益の取得",
                bool(trades),
                f"{len(trades)}件（実現損益 {trades[-1].realized_pnl}）"
                if trades
                else "取得できず",
            )
        except ZeroTradeError as exc:
            report.add("確定損益の取得", False, str(exc))


async def _check_stop_attached(
    broker: BaseBroker, symbol: str, position: Position, report: VerifyReport
) -> None:
    """ストップが取引所側に入ったかを確かめる。

    建玉に付いている場合と、別の条件注文として登録される場合がある。
    どちらでも「逆行したら止まる」ので、両方を見て判定する。

    ここが入っていないと、プロセスが落ちた瞬間に建玉が無防備になる。
    強制決済は StrategyRunner が動いている間しか働かない。
    """
    if position.stop_loss is not None:
        report.add("ストップの添付", True, f"建玉に {position.stop_loss} で設定済み")
        return

    try:
        orders = await broker.get_open_orders(symbol)
    except ZeroTradeError as exc:
        report.add("ストップの添付", False, f"確認できませんでした: {exc}")
        return

    protective = [o for o in orders if o.stop_price is not None or o.stop_loss is not None]
    if protective:
        report.add("ストップの添付", True, f"条件注文として {len(protective)}件 登録済み")
        return

    # 「無い」のか「読めていない」のかを切り分けられるよう、見えた注文数を残す。
    # --log-level DEBUG を付けると取引所の生応答も出る。
    report.add(
        "ストップの添付",
        False,
        f"取引所側にストップが見つかりません（未約定注文 {len(orders)}件を確認）。"
        f"この建玉は無防備です",
    )


async def _ensure_flat(broker: BaseBroker, symbol: str, report: VerifyReport) -> None:
    """建玉を残したまま終わらせない。**ここは何があっても実行する。**"""
    try:
        if await _current_position(broker, symbol) is None:
            return
    except BrokerError:
        return

    logger.warning("建玉が残っています。決済を試みます: %s", symbol)
    try:
        await broker.close_position(symbol)
        if not await _wait_until_flat(broker, symbol):
            report.position_left_open = True
            logger.error(
                "建玉を決済できませんでした。取引所の画面から手仕舞ってください: %s", symbol
            )
    except ZeroTradeError as exc:
        report.position_left_open = True
        logger.error("建玉の決済に失敗しました: %s（手動で決済してください）", exc)
