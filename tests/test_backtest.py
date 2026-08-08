"""バックテストエンジンのテスト。

最重要の検証は2つ。

1. 時計が相場時間で動くこと（実時間だと2年ぶんを数秒で流したときに
   日次損失上限が一度もリセットされず、最初の停止で終わってしまう）
2. 本番と同じリスク検査を通ること（バックテスト専用の抜け道が無いこと）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zerotrade.backtest import run_backtest, split_candles
from zerotrade.backtest.engine import SimulationClock, default_database
from zerotrade.backtest.optimize import (
    optimize,
    parse_param_spec,
    score_by_recovery_factor,
)
from zerotrade.data.historical import synthetic_candles
from zerotrade.errors import ConfigError
from zerotrade.models import Candle
from zerotrade.settings import Settings, StrategySettings
from zerotrade.store import Store

BASE = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "mode": "backtest",
            "symbols": ["USD_JPY"],
            "state_dir": "state",
            "broker": {"name": "paper", "initial_balance": "1000000"},
            "risk": {"reset_timezone": "UTC"},
            "sizing": {"min_quantity": "1000", "quantity_step": "1000"},
            "strategy": {
                "name": "sma_rsi",
                "params": {"fast_period": 5, "slow_period": 12, "rsi_period": 5, "atr_period": 5},
            },
            "notifications": {"console": False},
            "store": {"enabled": False},
        }
    )


def _candles(count: int = 600, *, seed: int = 7, interval_minutes: int = 5) -> list[Candle]:
    return synthetic_candles(
        "USD_JPY",
        count=count,
        volatility=0.002,
        drift=0.0003,
        seed=seed,
        start=BASE,
        interval=timedelta(minutes=interval_minutes),
    )


# ------------------------------------------------------------ 時計


def test_シミュレーション時計は設定した時刻を返す() -> None:
    clock = SimulationClock(BASE)
    assert clock() == BASE
    clock.now = BASE + timedelta(days=1)
    assert clock() == BASE + timedelta(days=1)


async def test_相場時間で日次カウンタがリセットされる(settings: Settings) -> None:
    """1時間足で60日ぶんを流し、日をまたいでも取引が続くこと。

    実時間の時計だと全体が数秒で終わるので日付が変わらず、
    一度停止したらそこで終わってしまう。
    """
    candles = _candles(count=1400, seed=3, interval_minutes=60)
    result = await run_backtest(settings, {"USD_JPY": candles})

    span = result.end - result.start if result.start and result.end else timedelta()
    assert span > timedelta(days=30), "相場時間が進んでいない"

    # トレードの決済時刻も相場時間になっていること（実行時の実時間ではない）。
    if result.trades:
        assert result.trades[0].closed_at.year == BASE.year
        assert all(t.closed_at >= BASE for t in result.trades)


async def test_トレードの時刻が足の時刻に一致する(settings: Settings) -> None:
    candles = _candles(count=500, seed=11)
    result = await run_backtest(settings, {"USD_JPY": candles})

    times = {c.timestamp for c in candles}
    for trade in result.trades:
        assert trade.closed_at in times, "決済時刻が足の時刻に載っていない"


# ------------------------------------------------------------ 実行


async def test_バックテストが一周する(settings: Settings) -> None:
    result = await run_backtest(settings, {"USD_JPY": _candles()})

    assert result.bars > 0
    assert result.stats.signals > 0
    assert result.stats.entries > 0, "エントリーが1件も無い"
    assert result.summary.trades > 0
    assert result.initial_equity == Decimal(1_000_000)
    assert "損益" in result.describe()


async def test_同じ入力からは同じ結果になる(settings: Settings) -> None:
    """乱数や実時間が混ざっていないことの確認。"""
    candles = _candles(seed=21)
    first = await run_backtest(settings, {"USD_JPY": candles})
    second = await run_backtest(settings, {"USD_JPY": candles})

    assert first.summary.net_pnl == second.summary.net_pnl
    assert first.summary.trades == second.summary.trades
    assert first.stats.entries == second.stats.entries


async def test_本番と同じリスク検査を通る(settings: Settings) -> None:
    """require_stop_loss をバックテストが迂回していないこと。"""
    tightened = settings.model_copy(
        update={"risk": settings.risk.model_copy(update={"max_risk_per_trade": Decimal("0.0001")})}
    )
    result = await run_backtest(tightened, {"USD_JPY": _candles()})

    # リスク上限を極端に絞れば、サイズが最小単位に届かず見送りになる。
    assert result.stats.entries == 0
    assert result.rejections, "却下が1件も記録されていない"


async def test_ポジション数の上限が効く(settings: Settings) -> None:
    limited = settings.model_copy(
        update={"risk": settings.risk.model_copy(update={"max_daily_trades": 1})}
    )
    result = await run_backtest(limited, {"USD_JPY": _candles(count=800)})
    assert result.rejections.get("max_daily_trades", 0) > 0


async def test_足が足りなければConfigError(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        await run_backtest(settings, {"USD_JPY": _candles(count=5)})


async def test_銘柄が欠けていればConfigError(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="足が与えられていない"):
        await run_backtest(settings, {"EUR_JPY": _candles()})


async def test_結果を記録層へ書き出せる(settings: Settings, tmp_path: Path) -> None:
    db = tmp_path / "bt.db"
    result = await run_backtest(settings, {"USD_JPY": _candles()}, database=db)

    assert db.is_file()
    assert result.database == db
    with Store.open_for_read(db) as store:
        assert len(store.trades(limit=999)) == result.summary.trades
        kinds = {e.kind for e in store.events()}
        assert {"backtest_start", "backtest_end"} <= kinds


async def test_パラメータを上書きできる(settings: Settings) -> None:
    result = await run_backtest(
        settings, {"USD_JPY": _candles()}, strategy_params={"fast_period": 3}
    )
    assert result.params["fast_period"] == 3


def test_書き出し先は本番の記録と分かれる(settings: Settings) -> None:
    """バックテスト結果が実運用の成績に混ざると台無しになる。"""
    path = default_database(settings, "backtest")
    assert path != settings.database_path
    assert "backtests" in path.parts


# ------------------------------------------------------------ 期間分割


def test_足を前半と後半に分ける() -> None:
    candles = _candles(count=100)
    head, tail = split_candles(candles, 0.7)

    assert len(head) == 70
    assert len(tail) == 30
    assert head[-1].timestamp < tail[0].timestamp


@pytest.mark.parametrize("ratio", [0.0, 1.0, -0.5, 1.5])
def test_不正な分割比は拒否される(ratio: float) -> None:
    with pytest.raises(ValueError, match="ratio"):
        split_candles(_candles(count=10), ratio)


# ------------------------------------------------------------ パラメータ掃引


def test_パラメータ指定を解釈できる() -> None:
    grid = parse_param_spec(["fast_period=5,10,20", "atr_stop_multiplier=1.5,2.0"])
    assert grid == {
        "fast_period": [5, 10, 20],
        "atr_stop_multiplier": [1.5, 2.0],
    }


def test_真偽値と文字列も解釈できる() -> None:
    grid = parse_param_spec(["allow_short=true,false"])
    assert grid == {"allow_short": [True, False]}


@pytest.mark.parametrize("spec", ["fast_period", "=5,10", "fast_period="])
def test_不正なパラメータ指定は拒否される(spec: str) -> None:
    with pytest.raises(ConfigError, match="形式が不正"):
        parse_param_spec([spec])


async def test_掃引はinとoutの両方を出す(settings: Settings) -> None:
    """全期間で最適化した数字だけを見せないこと。"""
    results = await optimize(
        settings,
        {"USD_JPY": _candles(count=700)},
        {"fast_period": [4, 6]},
        top=2,
    )

    assert len(results) == 2
    for entry in results:
        out = entry.out_of_sample
        assert out is not None, "後半での答え合わせが無い"
        assert entry.in_sample.end is not None and out.start is not None
        assert entry.in_sample.end < out.start, "in と out の期間が重なっている"
        assert "in :" in entry.describe()
        assert "out:" in entry.describe()


async def test_掃引はスコア順に並ぶ(settings: Settings) -> None:
    results = await optimize(
        settings, {"USD_JPY": _candles(count=700)}, {"fast_period": [3, 5, 7]}, top=3
    )
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


async def test_掃引パラメータが空なら拒否(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="指定されていません"):
        await optimize(settings, {"USD_JPY": _candles()}, {})


async def test_後半で負ければis_robustがFalse(settings: Settings) -> None:
    results = await optimize(
        settings, {"USD_JPY": _candles(count=700)}, {"fast_period": [4]}, top=1
    )
    entry = results[0]
    assert entry.out_of_sample is not None
    assert entry.is_robust == (entry.out_of_sample.summary.net_pnl > 0)


def test_トレードが少なすぎる結果は強く減点される(settings: Settings) -> None:
    """3件だけ勝った組み合わせが1位になると、掃引そのものが無意味になる。"""
    from zerotrade.backtest.engine import BacktestResult
    from zerotrade.core.runner import RunnerStats
    from zerotrade.store.models import PerformanceSummary

    def _result(trades: int, pnl: str, drawdown: str) -> BacktestResult:
        return BacktestResult(
            summary=PerformanceSummary(
                trades=trades,
                wins=trades,
                net_pnl=Decimal(pnl),
                max_drawdown=Decimal(drawdown),
            ),
            trades=[],
            stats=RunnerStats(),
            bars=100,
            start=BASE,
            end=BASE,
            initial_equity=Decimal(1_000_000),
            final_equity=Decimal(1_000_000),
            halted=None,
        )

    assert score_by_recovery_factor(_result(3, "100000", "1000")) < 0
    assert score_by_recovery_factor(_result(50, "100000", "50000")) == Decimal(2)


def test_ドローダウンが大きいほどスコアは下がる() -> None:
    """純損益だけで並べると、途中で口座を半分溶かす経路が上位に来る。"""
    from zerotrade.backtest.engine import BacktestResult
    from zerotrade.core.runner import RunnerStats
    from zerotrade.store.models import PerformanceSummary

    def _result(pnl: str, drawdown: str) -> BacktestResult:
        return BacktestResult(
            summary=PerformanceSummary(
                trades=30, wins=15, net_pnl=Decimal(pnl), max_drawdown=Decimal(drawdown)
            ),
            trades=[],
            stats=RunnerStats(),
            bars=100,
            start=BASE,
            end=BASE,
            initial_equity=Decimal(1_000_000),
            final_equity=Decimal(1_000_000),
            halted=None,
        )

    smooth = score_by_recovery_factor(_result("100000", "10000"))
    bumpy = score_by_recovery_factor(_result("120000", "500000"))
    assert smooth > bumpy


# ------------------------------------------------------------ 取引コスト


async def test_スプレッドが広いほど成績は悪化する(settings: Settings) -> None:
    """短期戦略ではスプレッドが成績を支配する。

    実データ（USD/JPY 2024年・5分足）では 0.3銭で -8%、2銭で -48% と
    6倍近い差が出た。設定値がブローカーの実勢とずれていると、
    検証結果そのものが意味を失う。
    """
    candles = _candles(count=900, seed=13)

    results = {}
    for spread in ("0", "0.02"):
        tuned = settings.model_copy(
            update={"broker": settings.broker.model_copy(update={"spread": Decimal(spread)})}
        )
        results[spread] = await run_backtest(tuned, {"USD_JPY": candles})

    assert results["0"].summary.trades > 0
    assert results["0.02"].summary.net_pnl < results["0"].summary.net_pnl


# ------------------------------------------------------------ ベンチマーク


async def test_単純保有との比較が出る(settings: Settings) -> None:
    """上昇相場ではゼロを基準にすると「勝った」と錯覚しやすい。"""
    rising = synthetic_candles(
        "USD_JPY",
        count=600,
        volatility=0.001,
        drift=0.002,
        seed=31,
        start=BASE,
        interval=timedelta(hours=1),
    )
    result = await run_backtest(settings, {"USD_JPY": rising})

    assert result.buy_and_hold_return > 0, "上昇相場なのに単純保有がプラスでない"
    assert result.beats_buy_and_hold == (result.return_ratio > result.buy_and_hold_return)
    assert "単純保有" in result.describe_vs_benchmark()


async def test_横ばいなら単純保有はほぼゼロ(settings: Settings) -> None:
    flat = synthetic_candles(
        "USD_JPY",
        count=600,
        volatility=0.0005,
        drift=0.0,
        seed=41,
        start=BASE,
        interval=timedelta(hours=1),
    )
    result = await run_backtest(settings, {"USD_JPY": flat})
    assert abs(result.buy_and_hold_return) < Decimal("0.5")


async def test_足種が食い違うと警告する(caplog: pytest.LogCaptureFixture) -> None:
    """H1のCSVを設定M5のまま検証しても黙って通ってしまう事故を防ぐ。"""
    import logging

    from zerotrade.backtest.engine import warn_on_granularity_mismatch

    settings = Settings(symbols=["USD_JPY"])  # granularity は既定の M5
    hourly = _candles(count=80, interval_minutes=60)

    with caplog.at_level(logging.WARNING):
        warn_on_granularity_mismatch(settings, {"USD_JPY": hourly})

    assert any("足の間隔" in r.message for r in caplog.records)


async def test_足種が揃っていれば警告しない(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from zerotrade.backtest.engine import warn_on_granularity_mismatch

    settings = Settings(
        symbols=["USD_JPY"],
        strategy=StrategySettings(name="sma_rsi", granularity="H1"),
    )
    hourly = _candles(count=80, interval_minutes=60)

    with caplog.at_level(logging.WARNING):
        warn_on_granularity_mismatch(settings, {"USD_JPY": hourly})

    assert not any("足の間隔" in r.message for r in caplog.records)
