"""StrategyRunner のエンドツーエンドテスト。

シグナル生成 → サイズ決定 → リスク検査 → 発注 → ストップ/利確 →
損益確定 → 損失上限による停止、までを PaperBroker 上で通しで確認する。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import FakeClock
from zerotrade.app import build_application
from zerotrade.brokers.paper import PaperBroker
from zerotrade.core.notifier import Level, Notifier
from zerotrade.core.orders import OrderManager
from zerotrade.core.risk import RiskManager
from zerotrade.core.runner import StrategyRunner
from zerotrade.core.sizing import PositionSizer
from zerotrade.data.feed import BrokerFeed
from zerotrade.data.historical import synthetic_candles
from zerotrade.models import Candle
from zerotrade.settings import Settings
from zerotrade.store import Store
from zerotrade.strategies import create_strategy
from zerotrade.strategies.base import Strategy


class RecordingNotifier(Notifier):
    """送られた通知を溜めておくだけの通知先。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, message: str, *, level: Level = "info") -> None:
        self.messages.append((level, message))

    def joined(self) -> str:
        return "\n".join(m for _, m in self.messages)


def _build(
    settings: Settings,
    *,
    clock: FakeClock,
    candles: dict[str, list[Candle]] | None = None,
    notifier: Notifier | None = None,
    strategy: Strategy | None = None,
    store: Store | None = None,
) -> tuple[StrategyRunner, PaperBroker, RiskManager]:
    broker = PaperBroker(
        list(settings.symbols),
        initial_balance=settings.broker.initial_balance,
        spread=settings.broker.spread,
        leverage=settings.risk.assumed_leverage,
        contract_size=settings.sizing.contract_size,
        candles=candles,
        warmup_bars=120,
    )
    risk = RiskManager(settings.risk, contract_size=settings.sizing.contract_size, clock=clock)
    runner = StrategyRunner(
        settings=settings,
        broker=broker,
        feed=BrokerFeed(broker),
        strategy=strategy or create_strategy(settings.strategy.name, settings.strategy.params),
        risk=risk,
        sizer=PositionSizer(settings.sizing, settings.risk),
        orders=OrderManager(broker, risk),
        notifier=notifier or RecordingNotifier(),
        store=store,
    )
    return runner, broker, risk


async def test_ペーパートレードが一周する(paper_settings: Settings, clock: FakeClock) -> None:
    """トレンドのある相場で、実際にエントリーと決済が発生することを確認する。"""
    notifier = RecordingNotifier()
    runner, broker, _risk = _build(
        paper_settings,
        clock=clock,
        candles={
            "USD_JPY": synthetic_candles(
                "USD_JPY", count=600, volatility=0.002, drift=0.0004, seed=7
            )
        },
        notifier=notifier,
    )

    stats = await runner.run(max_iterations=400)

    assert stats.iterations == 400
    assert stats.signals > 0
    assert stats.entries > 0, "400ループ回してエントリーが1件も無いのは異常"
    assert stats.exits > 0, "決済まで到達していない"
    assert stats.errors == 0

    trades = await broker.get_closed_trades()
    assert len(trades) > 0
    assert "ZeroTrade を開始しました" in notifier.joined()


async def test_全てのエントリーがリスク上限を守っている(
    paper_settings: Settings, clock: FakeClock
) -> None:
    """このテストが落ちたら実弾を入れてはいけない。"""
    runner, _broker, _ = _build(
        paper_settings,
        clock=clock,
        candles={
            "USD_JPY": synthetic_candles(
                "USD_JPY", count=600, volatility=0.002, drift=0.0004, seed=7
            )
        },
    )
    await runner.run(max_iterations=400)

    limit = Decimal(1_000_000) * paper_settings.risk.max_risk_per_trade
    checked = 0

    for order in runner.orders.orders.values():
        if order.metadata.get("strategy") is None:
            continue  # 決済注文
        if order.stop_loss is None or order.average_price is None:
            continue
        risk_amount = abs(order.average_price - order.stop_loss) * order.quantity
        # equity は取引につれて増減するが、初期資金基準の上限を大きく超えないこと。
        assert risk_amount <= limit * Decimal("1.2"), (
            f"{order.symbol} のリスク {risk_amount} が上限 {limit} を超えている"
        )
        checked += 1

    assert checked > 0, "検査対象のエントリーが1件も無い"


async def test_ストップ無しの戦略ではエントリーできない(
    paper_settings: Settings, clock: FakeClock
) -> None:
    """require_stop_loss が実行ループ全体で効いていることの確認。"""
    from zerotrade.models import Signal, SignalAction
    from zerotrade.strategies.base import Strategy, StrategyContext

    class NoStopStrategy(Strategy):
        name = "test_no_stop"
        warmup_bars = 1

        def generate(self, context: StrategyContext) -> Signal:
            if context.position is not None:
                return self.hold(context)
            return Signal(
                symbol=context.symbol,
                action=SignalAction.ENTER_LONG,
                strategy=self.name,
                stop_loss=None,  # ストップを付けない
            )

    runner, broker, _ = _build(paper_settings, clock=clock, strategy=NoStopStrategy())

    stats = await runner.run(max_iterations=20)

    assert stats.entries == 0
    await broker.connect()  # run() の終了時に切断されるため繋ぎ直す
    assert await broker.get_positions() == []
    # サイズ計算の段階（ストップ無し）で止まる。
    assert stats.rejections.get("zero_size", 0) > 0


async def test_損失上限に達すると新規が止まる(paper_settings: Settings, clock: FakeClock) -> None:
    notifier = RecordingNotifier()
    runner, broker, risk = _build(paper_settings, clock=clock, notifier=notifier)

    # 日次上限を超える損失を直接計上して停止状態を作る。
    risk.set_reference_equity(Decimal(1_000_000))
    risk.record_trade_closed("USD_JPY", Decimal(-50_000))
    assert risk.is_halted

    stats = await runner.run(max_iterations=100)

    assert stats.entries == 0
    await broker.connect()
    assert await broker.get_positions() == []


async def test_停止後も建玉は決済できる(paper_settings: Settings, clock: FakeClock) -> None:
    from zerotrade.models import OrderRequest, Side

    runner, broker, risk = _build(paper_settings, clock=clock)
    await broker.connect()
    await broker.place_order(OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(1_000)))

    risk.set_reference_equity(Decimal(1_000_000))
    risk.record_trade_closed("USD_JPY", Decimal(-100_000))
    assert risk.is_halted

    position = (await broker.get_positions())[0]
    balance = await broker.get_balance()
    result = await runner.orders.close_position(position, balance=balance, positions=[position])

    assert result.submitted
    assert await broker.get_positions() == []


async def test_stopで安全に止まる(paper_settings: Settings, clock: FakeClock) -> None:
    runner, _, _ = _build(paper_settings, clock=clock)
    runner.stop()
    stats = await runner.run()
    assert stats.iterations == 0


async def test_build_applicationで一式が組み立つ(paper_settings: Settings, tmp_path: Path) -> None:
    settings = paper_settings.model_copy(update={"state_dir": tmp_path})
    app = build_application(settings)

    assert app.broker.name == "paper"
    assert app.runner is not None
    stats = await app.runner.run(max_iterations=5)
    await app.aclose()

    assert stats.iterations == 5
    assert (tmp_path / "risk_state.json").exists(), "リスク状態が永続化されていない"


async def test_APIキー無しでもOANDA設定から起動できる(tmp_path: Path) -> None:
    """fallback_to_paper の動作確認。"""
    settings = Settings.model_validate(
        {
            "mode": "paper",
            "symbols": ["USD_JPY"],
            "state_dir": str(tmp_path),
            "broker": {"name": "oanda", "fallback_to_paper": True},
            "notifications": {"console": False},
        }
    )
    app = build_application(settings)
    assert app.broker.name == "paper"
    await app.aclose()


async def test_fallback無効なら起動に失敗する(tmp_path: Path) -> None:
    from zerotrade.errors import BrokerError

    settings = Settings.model_validate(
        {
            "mode": "paper",
            "symbols": ["USD_JPY"],
            "state_dir": str(tmp_path),
            "broker": {"name": "oanda", "fallback_to_paper": False},
            "notifications": {"console": False},
        }
    )
    with pytest.raises(BrokerError, match="認証情報"):
        build_application(settings)


# ------------------------------------------------------- 確定損益の推定


class NoHistoryBroker(PaperBroker):
    """決済履歴を返せないブローカー（多くの取引所がこれに当たる）。"""

    supports_closed_trades = False


async def test_決済履歴が無くても損失上限が働く(paper_settings: Settings, clock: FakeClock) -> None:
    """ここを素通りさせると、日次・週次の損失上限が一切働かなくなる。

    supports_closed_trades=False のブローカーでは、建玉の差分から
    確定損益を推定して RiskManager へ渡す必要がある。
    """
    from zerotrade.core.orders import OrderManager
    from zerotrade.core.risk import RiskManager
    from zerotrade.core.runner import StrategyRunner
    from zerotrade.core.sizing import PositionSizer
    from zerotrade.data.feed import BrokerFeed
    from zerotrade.models import OrderRequest, Side
    from zerotrade.strategies import create_strategy

    broker = NoHistoryBroker(
        ["USD_JPY"],
        initial_balance=Decimal(1_000_000),
        candles={"USD_JPY": synthetic_candles("USD_JPY", count=400, seed=3)},
        warmup_bars=120,
    )
    risk = RiskManager(paper_settings.risk, clock=clock)
    runner = StrategyRunner(
        settings=paper_settings,
        broker=broker,
        feed=BrokerFeed(broker),
        strategy=create_strategy("sma_rsi", paper_settings.strategy.params),
        risk=risk,
        sizer=PositionSizer(paper_settings.sizing, paper_settings.risk),
        orders=OrderManager(broker, risk),
        notifier=RecordingNotifier(),
    )

    await broker.connect()
    risk.set_reference_equity(Decimal(1_000_000))

    # 建玉を持った状態を作り、スナップショットへ載せる。
    await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(10_000))
    )
    await runner.step()
    assert runner._previous_positions, "建玉のスナップショットが取れていない"

    # 建玉を消す（取引所側でストップに掛かった状況に相当）
    await broker.close_position("USD_JPY")
    before = risk.state.daily_pnl
    await runner.step()

    assert risk.state.daily_pnl != before, "確定損益が RiskManager へ届いていない"


async def test_履歴を返せるブローカーでは推定しない(
    paper_settings: Settings, clock: FakeClock
) -> None:
    """二重計上を防ぐ。PaperBroker は正確な履歴を返せる。"""
    runner, broker, _risk = _build(paper_settings, clock=clock)
    await runner.run(max_iterations=30)

    await broker.connect()
    trades = await broker.get_closed_trades()
    # 推定経路が動いていれば "inferred" が混ざる。
    assert all(t.reason != "inferred" for t in trades)
