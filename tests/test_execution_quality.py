"""約定品質（滑りの実測と MFE/MAE）のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zerotrade.brokers.paper import PaperBroker
from zerotrade.models import (
    Candle,
    ClosedTrade,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from zerotrade.store import Store, summarize_execution
from zerotrade.store.models import TradeRow

START = datetime(2026, 8, 8, tzinfo=UTC)


def _candles(symbol: str, rows: list[tuple[int, int, int, int]]) -> list[Candle]:
    """(open, high, low, close) の並びから足を作る。"""
    return [
        Candle(
            symbol=symbol,
            timestamp=START + timedelta(hours=i),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
        )
        for i, (o, h, low, c) in enumerate(rows)
    ]


# ---------------------------------------------------------------- 滑り


def test_買いは高く約定するほど滑りが正になる() -> None:
    order = Order(
        client_order_id="a",
        symbol="BTC_USDT",
        side=Side.BUY,
        quantity=Decimal(1),
        order_type=OrderType.MARKET,
        average_price=Decimal("100.5"),
        reference_price=Decimal(100),
    )
    assert order.slippage == Decimal("0.5")
    assert order.slippage_bp == Decimal(50)


def test_売りは安く約定するほど滑りが正になる() -> None:
    """符号を方向で揃えないと、買いと売りを平均した瞬間に打ち消し合う。"""
    order = Order(
        client_order_id="a",
        symbol="BTC_USDT",
        side=Side.SELL,
        quantity=Decimal(1),
        order_type=OrderType.MARKET,
        average_price=Decimal("99.5"),
        reference_price=Decimal(100),
    )
    assert order.slippage == Decimal("0.5")


def test_想定価格が無ければ滑りは測れない() -> None:
    order = Order(
        client_order_id="a",
        symbol="BTC_USDT",
        side=Side.BUY,
        quantity=Decimal(1),
        order_type=OrderType.MARKET,
        average_price=Decimal(100),
    )
    assert order.slippage is None
    assert order.slippage_bp is None


@pytest.mark.asyncio
async def test_PaperBroker_は想定価格を注文へ引き継ぐ() -> None:
    broker = PaperBroker(["BTC_USDT"], slippage=Decimal(1), spread=Decimal(0))
    await broker.connect()
    order = await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.BUY,
            quantity=Decimal(1),
            reference_price=Decimal(100),
        )
    )
    assert order.reference_price == Decimal(100)
    assert order.status is OrderStatus.FILLED
    # 設定した滑りぶんだけ不利側で約定している。
    assert order.average_price is not None
    assert order.slippage is not None


# ---------------------------------------------------------------- MFE / MAE


@pytest.mark.asyncio
async def test_決済トレードに建玉中の振れ幅が入る() -> None:
    """含み益 +50 まで伸びてから、+10 で決済したケース。"""
    symbol = "BTC_USDT"
    broker = PaperBroker(
        [symbol],
        spread=Decimal(0),
        candles={
            symbol: _candles(
                symbol,
                [
                    (100, 100, 100, 100),  # ウォームアップ
                    (100, 150, 90, 100),  # 建玉中に大きく往復する
                    (100, 110, 100, 110),
                ],
            )
        },
        warmup_bars=1,
    )
    await broker.connect()

    await broker.place_order(
        OrderRequest(symbol=symbol, side=Side.BUY, quantity=Decimal(1), take_profit=Decimal(110))
    )
    # 足を進めて利確させる。
    await broker.get_ohlcv(symbol)
    await broker.get_ohlcv(symbol)

    trades = await broker.get_closed_trades()
    assert len(trades) == 1
    assert trades[0].mfe == Decimal(50)  # 150 まで伸びた
    assert trades[0].mae == Decimal(-10)  # 90 まで落ちた
    assert trades[0].realized_pnl == Decimal(10)


def test_取り切り率は含み益のピークに対する割合() -> None:
    trade = ClosedTrade(
        symbol="BTC_USDT",
        side=Side.BUY,
        quantity=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(110),
        realized_pnl=Decimal(10),
        opened_at=START,
        mfe=Decimal(50),
        mae=Decimal(-10),
    )
    assert trade.capture_ratio == Decimal("0.2")


def test_含み益にならなかったトレードの取り切り率はNone() -> None:
    trade = ClosedTrade(
        symbol="BTC_USDT",
        side=Side.BUY,
        quantity=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(90),
        realized_pnl=Decimal(-10),
        opened_at=START,
        mfe=Decimal(0),
        mae=Decimal(-10),
    )
    assert trade.capture_ratio is None


# ---------------------------------------------------------------- 集計


def _row(pnl: str, mfe: str | None, mae: str | None) -> TradeRow:
    return TradeRow(
        symbol="BTC_USDT",
        side="buy",
        quantity=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(100),
        realized_pnl=Decimal(pnl),
        opened_at=START,
        closed_at=START,
        reason="signal",
        strategy="test",
        mfe=None if mfe is None else Decimal(mfe),
        mae=None if mae is None else Decimal(mae),
    )


def test_記録の無いトレードは集計から外す() -> None:
    """未記録を0として混ぜると、平均が実態より良く見える。"""
    quality = summarize_execution([], [_row("10", "20", "-5"), _row("-10", None, None)])
    assert quality.trades_with_excursion == 1
    assert quality.average_mfe == Decimal(20)


def test_取り切り率は比の平均ではなく合計どうしの比で出す() -> None:
    """含み益がほぼ0のトレードで分母が潰れても壊れないこと。"""
    quality = summarize_execution(
        [],
        [
            _row("90", "100", "-10"),  # よく取れている
            _row("-50", "0.01", "-60"),  # 比を取ると -5000 になる外れ値
        ],
    )
    assert quality.average_capture_ratio is not None
    # (90 - 50) / (100 + 0.01) ≒ 0.4。外れ値に引きずられない。
    assert Decimal("0.39") < quality.average_capture_ratio < Decimal("0.41")


def test_勝ちトレードの平均MAEを分けて出す() -> None:
    quality = summarize_execution(
        [], [_row("10", "20", "-30"), _row("-10", "5", "-15"), _row("40", "50", "-10")]
    )
    assert quality.winners_average_mae == Decimal(-20)  # (-30 + -10) / 2
    assert quality.trades_with_excursion == 3


def test_記録が空でも壊れない() -> None:
    quality = summarize_execution([], [])
    assert quality.fills == 0
    assert quality.average_capture_ratio is None
    assert "まだ記録がありません" in quality.describe()


# ---------------------------------------------------------------- 記録層


def test_記録層に滑りとMFE_MAEが往復する(tmp_path: Path) -> None:
    with Store(tmp_path / "zt.db") as store:
        store.record_order(
            Order(
                client_order_id="a",
                symbol="BTC_USDT",
                side=Side.BUY,
                quantity=Decimal(1),
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                average_price=Decimal("100.2"),
                reference_price=Decimal(100),
            )
        )
        store.record_trade(
            ClosedTrade(
                symbol="BTC_USDT",
                side=Side.BUY,
                quantity=Decimal(1),
                entry_price=Decimal(100),
                exit_price=Decimal(110),
                realized_pnl=Decimal(10),
                opened_at=START,
                closed_at=START,
                mfe=Decimal(50),
                mae=Decimal(-10),
            )
        )

        slippage = store.slippage()
        assert len(slippage) == 1
        assert slippage[0].slippage_bp == Decimal(20)

        quality = store.execution_quality()
        assert quality.fills == 1
        assert quality.average_capture_ratio == Decimal("0.2")


def test_古いDBにも列を足して読めるようにする(tmp_path: Path) -> None:
    """稼働中の記録を作り直さずに使い続けられること。"""
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE,"
            " symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,"
            " entry_price TEXT NOT NULL, exit_price TEXT NOT NULL, realized_pnl TEXT NOT NULL,"
            " opened_at TEXT NOT NULL, closed_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
            " strategy TEXT NOT NULL DEFAULT '');"
            "INSERT INTO trades (dedup_key, symbol, side, quantity, entry_price, exit_price,"
            " realized_pnl, opened_at, closed_at)"
            f" VALUES ('k', 'BTC_USDT', 'buy', '1', '100', '110', '10',"
            f" '{START.isoformat()}', '{START.isoformat()}');"
        )

    with Store(path) as store:
        trades = store.trades()
        assert len(trades) == 1
        assert trades[0].mfe is None  # 古い行には無い

        store.record_trade(
            ClosedTrade(
                symbol="BTC_USDT",
                side=Side.BUY,
                quantity=Decimal(1),
                entry_price=Decimal(100),
                exit_price=Decimal(120),
                realized_pnl=Decimal(20),
                opened_at=START,
                closed_at=START + timedelta(hours=1),
                mfe=Decimal(30),
                mae=Decimal(-4),
            )
        )
        assert store.trades()[0].mfe == Decimal(30)
