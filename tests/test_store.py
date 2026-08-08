"""SQLite 記録層のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import START, make_request
from zerotrade.models import (
    Balance,
    ClosedTrade,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Signal,
    SignalAction,
)
from zerotrade.store import Store, summarize
from zerotrade.store.models import TradeRow


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "zerotrade.db")


def _trade(
    pnl: str, *, closed_at: datetime | None = None, trade_id: str = "t1", reason: str = "stop_loss"
) -> ClosedTrade:
    return ClosedTrade(
        symbol="USD_JPY",
        side=Side.BUY,
        quantity=Decimal(10_000),
        entry_price=Decimal("150.00"),
        exit_price=Decimal("149.00"),
        realized_pnl=Decimal(pnl),
        opened_at=closed_at or START,
        closed_at=closed_at or START,
        trade_id=trade_id,
        reason=reason,
    )


def _row(pnl: str) -> TradeRow:
    return TradeRow(
        symbol="USD_JPY",
        side="buy",
        quantity=Decimal(1),
        entry_price=Decimal(1),
        exit_price=Decimal(1),
        realized_pnl=Decimal(pnl),
        opened_at=START,
        closed_at=START,
        reason="",
        strategy="",
    )


# ------------------------------------------------------------ 基本


def test_DBが作られWALで開かれる(tmp_path: Path) -> None:
    """別プロセスから読むため WAL であることが前提になっている。"""
    path = tmp_path / "nested" / "zerotrade.db"
    with Store(path) as store:
        mode = next(iter(store._conn.execute("PRAGMA journal_mode")))[0]
    assert path.is_file()
    assert mode.lower() == "wal"


def test_存在しないDBの読み取りはエラー(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Store.open_for_read(tmp_path / "missing.db")


# ------------------------------------------------------------ トレード


def test_トレードを記録して読み出せる(store: Store) -> None:
    store.record_trade(_trade("-4500"))
    rows = store.trades()

    assert len(rows) == 1
    assert rows[0].realized_pnl == Decimal("-4500")
    assert rows[0].reason == "stop_loss"
    assert not rows[0].is_win


def test_同じトレードは二重計上されない(store: Store) -> None:
    """ブローカーの決済履歴APIは同じトレードを何度も返す。"""
    trade = _trade("-4500")
    for _ in range(5):
        store.record_trade(trade)
    assert len(store.trades()) == 1


def test_金額はDecimalのまま往復する(store: Store) -> None:
    """REAL で保存すると丸め誤差が入る。文字列で保存していることの確認。"""
    store.record_trade(_trade("-4500.123456789012345678"))
    assert store.trades()[0].realized_pnl == Decimal("-4500.123456789012345678")


def test_since以降のトレードだけ取れる(store: Store) -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 6, 1, tzinfo=UTC)
    store.record_trade(_trade("100", closed_at=old, trade_id="old"))
    store.record_trade(_trade("200", closed_at=new, trade_id="new"))

    rows = store.trades(since=datetime(2026, 3, 1, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].realized_pnl == Decimal("200")


# ------------------------------------------------------------ 注文・却下


def test_注文は最新状態で上書きされる(store: Store) -> None:
    order = Order(
        client_order_id="zt-1",
        symbol="USD_JPY",
        side=Side.BUY,
        quantity=Decimal(1000),
        order_type=OrderType.LIMIT,
        status=OrderStatus.OPEN,
        metadata={"strategy": "sma_rsi", "reason": "テスト"},
    )
    store.record_order(order)

    order.status = OrderStatus.FILLED
    order.filled_quantity = Decimal(1000)
    order.average_price = Decimal("150.00")
    store.record_order(order)

    rows = list(store._conn.execute("SELECT * FROM orders"))
    assert len(rows) == 1
    assert rows[0]["status"] == "filled"
    assert rows[0]["average_price"] == "150.00"
    assert rows[0]["strategy"] == "sma_rsi"


def test_却下されたルールを集計できる(store: Store) -> None:
    store.record_rejection(make_request(), "max_risk_per_trade", "リスク超過")
    store.record_rejection(make_request(), "max_risk_per_trade", "リスク超過")
    store.record_rejection(make_request(), "atr_spike", "ATR急拡大")

    counts = store.rejection_counts()
    assert counts == {"max_risk_per_trade": 2, "atr_spike": 1}
    # 多い順に並んでいること。
    assert next(iter(counts)) == "max_risk_per_trade"


def test_却下の詳細を新しい順に読める(store: Store) -> None:
    store.record_rejection(make_request(), "require_stop_loss", "1件目")
    store.record_rejection(make_request(), "require_stop_loss", "2件目")

    rows = store.rejections()
    assert rows[0].detail == "2件目"
    assert rows[0].rule == "require_stop_loss"


# ------------------------------------------------------------ シグナル・equity


def test_シグナルを記録できる(store: Store) -> None:
    store.record_signal(
        Signal(
            symbol="USD_JPY",
            action=SignalAction.ENTER_LONG,
            strategy="sma_rsi",
            reason="ゴールデンクロス",
        )
    )
    rows = store.signals()
    assert rows[0].action == "enter_long"
    assert rows[0].reason == "ゴールデンクロス"


def test_equity推移は古い順に返る(store: Store) -> None:
    for i, value in enumerate((1000, 1100, 900)):
        store.record_equity(
            Balance(
                currency="JPY",
                equity=Decimal(value),
                available=Decimal(value),
                used_margin=Decimal(0),
                timestamp=START + timedelta(minutes=i),
            ),
            open_positions=i,
        )

    curve = store.equity_curve()
    assert [p.equity for p in curve] == [Decimal(1000), Decimal(1100), Decimal(900)]

    latest = store.latest_equity()
    assert latest is not None
    assert latest.equity == Decimal(900)


def test_記録が無ければlatest_equityはNone(store: Store) -> None:
    assert store.latest_equity() is None


def test_イベントを記録できる(store: Store) -> None:
    store.record_event("halt", "日次上限")
    rows = store.events()
    assert rows[0].kind == "halt"
    assert rows[0].detail == "日次上限"


# ------------------------------------------------------------ 集計


def test_成績を集計できる() -> None:
    summary = summarize([_row("100"), _row("-40"), _row("60"), _row("-20")])

    assert summary.trades == 4
    assert summary.wins == 2
    assert summary.losses == 2
    assert summary.net_pnl == Decimal(100)
    assert summary.win_rate == Decimal("0.5")
    assert summary.average_win == Decimal(80)
    assert summary.average_loss == Decimal(30)
    assert summary.profit_factor == Decimal(160) / Decimal(60)
    assert summary.expectancy == Decimal(25)


def test_最大ドローダウンは累積曲線の落ち込み() -> None:
    # 累積: 100 → 30 → 10 → 60。ピーク100から10まで落ちるので DD は 90。
    summary = summarize([_row("100"), _row("-70"), _row("-20"), _row("50")])
    assert summary.max_drawdown == Decimal(90)


def test_トレードが無ければ全項目ゼロ() -> None:
    summary = summarize([])
    assert summary.trades == 0
    assert summary.win_rate == 0
    assert summary.expectancy == 0
    assert summary.average_win == 0


def test_負けが無ければプロフィットファクタは未定義() -> None:
    """0除算を避けるため None を返す。1.0 や inf を返すと誤読される。"""
    summary = summarize([_row("100"), _row("50")])
    assert summary.profit_factor is None


def test_store経由の集計は古い順で計算される(store: Store) -> None:
    """trades() は新しい順に返すので、集計側で並べ直せていないと DD がずれる。"""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i, pnl in enumerate(("100", "-70", "-20", "50")):
        store.record_trade(_trade(pnl, closed_at=base + timedelta(hours=i), trade_id=f"t{i}"))

    summary = store.performance()
    assert summary.net_pnl == Decimal(60)
    assert summary.max_drawdown == Decimal(90)


# ------------------------------------------------------------ 堅牢性


def test_読み取り専用ストアは書き込まない(tmp_path: Path) -> None:
    path = tmp_path / "zerotrade.db"
    with Store(path) as writer:
        writer.record_trade(_trade("100"))

    with Store.open_for_read(path) as reader:
        reader.record_trade(_trade("-999", trade_id="t2"))
        assert len(reader.trades()) == 1, "読み取り専用で書き込まれてはいけない"


def test_書き込み中でも別接続から読める(tmp_path: Path) -> None:
    """ダッシュボードが取引プロセスと同時に動く前提の確認。"""
    path = tmp_path / "zerotrade.db"
    with Store(path) as writer:
        writer.record_trade(_trade("100"))
        with Store.open_for_read(path) as reader:
            assert len(reader.trades()) == 1
            writer.record_trade(_trade("200", trade_id="t2"))
            assert len(reader.trades()) == 2
