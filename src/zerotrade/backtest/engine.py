"""バックテストエンジン。

**本番と同じ経路を通す**のがこの実装の唯一にして最大の方針である。
戦略もサイズ決定もリスク検査も発注も、ライブ実行とまったく同じ
:class:`~zerotrade.core.runner.StrategyRunner` /
:class:`~zerotrade.core.risk.RiskManager` /
:class:`~zerotrade.core.orders.OrderManager` を通る。
バックテスト専用のロジックを別に書くと、そこに紛れ込んだ差異が
「検証では勝てたのに実弾では負ける」の温床になる。

ライブとの違いは3点だけに絞ってある:

1. 時計が相場時間になる（実時間だと2年ぶんを数秒で流したときに
   日次損失上限が一度もリセットされない）
2. ループ間の待機が無い
3. 通知を出さず、リスク状態もディスクに残さない

エンジンが差し込むのは「時計」と「ループの回し方」だけで、
判断の経路そのものには一切手を入れていない。
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from zerotrade.brokers.paper import PaperBroker
from zerotrade.core.notifier import NullNotifier
from zerotrade.core.orders import OrderManager
from zerotrade.core.risk import RiskManager
from zerotrade.core.runner import RunnerStats, StrategyRunner
from zerotrade.core.sizing import PositionSizer
from zerotrade.data.align import align_candles
from zerotrade.data.feed import BrokerFeed
from zerotrade.errors import ConfigError
from zerotrade.log import get_logger
from zerotrade.models import Candle, utcnow
from zerotrade.settings import Settings
from zerotrade.store import PerformanceSummary, Store, summarize
from zerotrade.store.models import TradeRow
from zerotrade.strategies import create_strategy

__all__ = ["BacktestResult", "SimulationClock", "run_backtest", "split_candles"]

logger = get_logger(__name__)

#: ウォームアップ後にこれだけのステップが取れないと検証として意味を持たない。
MIN_STEPS = 20


class SimulationClock:
    """相場時間を返す時計。

    :class:`RiskManager` に渡すことで、日次・週次カウンタのリセットが
    実時間ではなくローソク足の時刻で起きるようになる。
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """バックテスト1回ぶんの結果。"""

    summary: PerformanceSummary
    trades: list[TradeRow]
    stats: RunnerStats
    bars: int
    start: datetime | None
    end: datetime | None
    initial_equity: Decimal
    final_equity: Decimal
    halted: str | None
    rejections: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    database: Path | None = None
    """結果を書き出した記録層。``zerotrade report`` で読める。"""

    buy_and_hold_return: Decimal = Decimal(0)
    """同じ期間を単純に買って持っていた場合の騰落率。

    比較対象をゼロに置くと、上昇相場では「勝った」と錯覚しやすい。
    2024年のドル円は年間 +11.7% で、これを下回る戦略に労力を割く意味は薄い。
    """

    @property
    def return_ratio(self) -> Decimal:
        """初期資金に対する損益率。"""
        if self.initial_equity <= 0:
            return Decimal(0)
        return (self.final_equity - self.initial_equity) / self.initial_equity

    @property
    def beats_buy_and_hold(self) -> bool:
        """単純保有を上回ったか。"""
        return self.return_ratio > self.buy_and_hold_return

    def describe(self) -> str:
        """1行サマリ。掃引結果の一覧に使う。"""
        factor = self.summary.profit_factor
        return (
            f"損益 {self.summary.net_pnl:+,.0f} ({self.return_ratio:+.2%}) / "
            f"{self.summary.trades}件 / 勝率 {self.summary.win_rate:.0%} / "
            f"PF {'—' if factor is None else f'{factor:.2f}'} / "
            f"最大DD -{self.summary.max_drawdown:,.0f}"
            + (f" / 停止({self.halted})" if self.halted else "")
        )

    def describe_vs_benchmark(self) -> str:
        """単純保有との比較を含めた1行サマリ。"""
        mark = "○" if self.beats_buy_and_hold else "×"
        return f"{self.describe()} / 単純保有 {self.buy_and_hold_return:+.2%} → {mark}"


def split_candles(
    candles: Sequence[Candle], ratio: float = 0.7
) -> tuple[list[Candle], list[Candle]]:
    """足を前半（in-sample）と後半（out-of-sample）に分ける。

    パラメータ最適化は前半だけで行い、後半で答え合わせをする。
    全期間で最適化した数字は、ほぼ必ず過学習している。
    """
    if not 0 < ratio < 1:
        raise ValueError("ratio は 0 と 1 の間で指定してください")
    pivot = int(len(candles) * ratio)
    return list(candles[:pivot]), list(candles[pivot:])


def warn_on_granularity_mismatch(
    settings: Settings, series: Mapping[str, Sequence[Candle]]
) -> None:
    """CSV の足の間隔と ``strategy.granularity`` が食い違っていたら警告する。

    ここが食い違うと、**検証結果がライブ実行の何の保証にもならない**。
    H1 のCSVで検証して設定が M5 のままなら、同じパラメータでも別の戦略になる。
    落とさずに警告に留めるのは、掃引や分析でわざと別の足を流すことがあるため。
    """
    from zerotrade.data.importer import parse_granularity

    for symbol, candles in series.items():
        if len(candles) < 3:
            continue
        gaps = [
            (b.timestamp - a.timestamp).total_seconds()
            for a, b in itertools.pairwise(candles[:200])
        ]
        actual = min(g for g in gaps if g > 0) if any(g > 0 for g in gaps) else 0
        if actual <= 0:
            continue
        expected = parse_granularity(settings.strategy.granularity).total_seconds()
        if abs(actual - expected) > 1:
            logger.warning(
                "%s の足の間隔は %.0f 秒ですが、設定の strategy.granularity は %s (%.0f 秒) です。"
                "ライブ実行では設定側の足種が使われるため、この検証結果は保証になりません",
                symbol,
                actual,
                settings.strategy.granularity,
                expected,
            )


async def run_backtest(
    settings: Settings,
    candles: Mapping[str, Sequence[Candle]],
    *,
    strategy_params: Mapping[str, Any] | None = None,
    database: Path | None = None,
    label: str = "backtest",
) -> BacktestResult:
    """ヒストリカルデータ上で戦略を1回走らせる。

    Args:
        settings: 使用する設定。``symbols`` は ``candles`` のキーと揃っている必要がある。
        candles: 銘柄ごとの足（古い順）。
        strategy_params: 設定の戦略パラメータを上書きする値。掃引で使う。
        database: 結果の書き出し先。``None`` なら記録層を使わない。
        label: ログ表示用の名前。

    Returns:
        成績・トレード履歴・却下内訳を含む結果。

    Raises:
        ConfigError: 足が足りない、または銘柄が揃っていない場合。
    """
    missing = [s for s in settings.symbols if not candles.get(s)]
    if missing:
        raise ConfigError(f"足が与えられていない銘柄があります: {', '.join(missing)}")

    # 銘柄ごとに歯抜けの位置が違うため、共通の時刻へ揃えてから流す。
    series = align_candles({s: candles[s] for s in settings.symbols})
    warn_on_granularity_mismatch(settings, series)
    params = {**settings.strategy.params, **(strategy_params or {})}
    strategy = create_strategy(settings.strategy.name, params)

    # ウォームアップは戦略が要求する本数をそのまま使う。ここを削ると
    # 指標が不足データで計算され、戦略は HOLD を返し続けるだけになる。
    warmup = strategy.warmup_bars
    shortest = min(len(v) for v in series.values())
    required = warmup + MIN_STEPS
    if shortest < required:
        raise ConfigError(
            f"足が不足しています（最短 {shortest} 本 / "
            f"必要 {required} 本以上: ウォームアップ {warmup} + 最低 {MIN_STEPS} ステップ）"
        )

    reference = series[settings.symbols[0]]
    steps = len(reference) - warmup
    clock = SimulationClock(reference[warmup - 1].timestamp)

    broker = PaperBroker(
        list(settings.symbols),
        initial_balance=settings.broker.initial_balance,
        currency=settings.broker.account_currency,
        spread=settings.broker.spread,
        leverage=settings.risk.assumed_leverage,
        contract_size=settings.sizing.contract_size,
        candles=series,
        warmup_bars=warmup,
    )
    # state_path を渡さない＝リスク状態をディスクに残さない。
    # 掃引で何百回も走らせるので、本番の状態を汚してはいけない。
    risk = RiskManager(settings.risk, contract_size=settings.sizing.contract_size, clock=clock)
    store = Store(database) if database is not None else None

    runner = StrategyRunner(
        settings=settings,
        broker=broker,
        feed=BrokerFeed(broker),
        strategy=strategy,
        risk=risk,
        sizer=PositionSizer(settings.sizing, settings.risk),
        orders=OrderManager(broker, risk),
        notifier=NullNotifier(),
        store=store,
    )

    started = time.perf_counter()
    await broker.connect()
    initial = (await broker.get_balance()).equity
    risk.set_reference_equity(initial)
    if store is not None:
        store.record_event("backtest_start", f"{label} / {steps}本 / params={params}")

    try:
        for i in range(steps):
            # step() の中で足が1本進むので、その足の時刻を先に時計へ入れる。
            clock.now = reference[min(warmup + i, len(reference) - 1)].timestamp
            await runner.step()
    finally:
        final = (await broker.get_balance()).equity
        await broker.disconnect()

    trades = [
        TradeRow(
            symbol=t.symbol,
            side=str(t.side),
            quantity=t.quantity,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            realized_pnl=t.realized_pnl,
            opened_at=t.opened_at,
            closed_at=t.closed_at,
            reason=t.reason,
            strategy=strategy.name,
            mfe=t.mfe,
            mae=t.mae,
        )
        for t in await broker.get_closed_trades()
    ]
    summary = summarize(trades)

    if store is not None:
        store.record_event("backtest_end", f"{label} / {summary.net_pnl:+.0f}")
        store.close()

    result = BacktestResult(
        summary=summary,
        trades=trades,
        stats=runner.stats,
        bars=steps,
        start=reference[warmup].timestamp if steps else None,
        end=reference[-1].timestamp if steps else None,
        initial_equity=initial,
        final_equity=final,
        halted=risk.state.halt_reason,
        rejections=dict(runner.stats.rejections),
        elapsed_seconds=time.perf_counter() - started,
        params=params,
        database=database,
        buy_and_hold_return=_buy_and_hold_multi({s: v[warmup:] for s, v in series.items()}),
    )
    logger.info("[%s] %s（%d本 / %.1f秒）", label, result.describe(), steps, result.elapsed_seconds)
    return result


def _buy_and_hold(candles: Sequence[Candle]) -> Decimal:
    """同じ期間を単純に買って持っていた場合の騰落率。"""
    if len(candles) < 2 or candles[0].close <= 0:
        return Decimal(0)
    return (candles[-1].close - candles[0].close) / candles[0].close


def _buy_and_hold_multi(series: Mapping[str, Sequence[Candle]]) -> Decimal:
    """複数銘柄を等ウェイトで持っていた場合の騰落率。

    比較対象は「その戦略が触れた市場を、何も考えず等分に持つ」こと。
    分散を売りにする戦略ほど、この基準と比べないと意味を持たない。
    """
    ratios = [_buy_and_hold(v) for v in series.values() if len(v) >= 2]
    if not ratios:
        return Decimal(0)
    return sum(ratios, Decimal(0)) / Decimal(len(ratios))


def default_database(settings: Settings, label: str) -> Path:
    """バックテスト結果の既定の書き出し先。

    本番の記録（``state/zerotrade.db``）とは必ず分ける。
    混ぜると実際の運用成績が架空の数字で汚れる。
    """
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return settings.state_dir / "backtests" / f"{label}-{stamp}.db"
