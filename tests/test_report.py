"""HTMLレポート生成のテスト。

「サーバー不要・単体で開ける1枚のHTML」という前提が崩れていないか
（外部リソースを参照していないか）も確認する。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import START
from zerotrade.models import Balance, ClosedTrade, Side
from zerotrade.report import build_report, format_price, render_report
from zerotrade.store import Store, summarize
from zerotrade.store.models import EquityPoint, EventRow, PerformanceSummary, TradeRow


def _trade_row(pnl: str, *, symbol: str = "USD_JPY", reason: str = "stop_loss") -> TradeRow:
    return TradeRow(
        symbol=symbol,
        side="buy",
        quantity=Decimal(10_000),
        entry_price=Decimal("150.00"),
        exit_price=Decimal("149.00"),
        realized_pnl=Decimal(pnl),
        opened_at=START,
        closed_at=START,
        reason=reason,
        strategy="sma_rsi",
    )


def _equity(values: list[int]) -> list[EquityPoint]:
    return [
        EquityPoint(
            created_at=START + timedelta(minutes=i),
            equity=Decimal(v),
            used_margin=Decimal(0),
            open_positions=0,
        )
        for i, v in enumerate(values)
    ]


def _render(**overrides: object) -> str:
    defaults: dict[str, object] = {
        "summary": PerformanceSummary(),
        "trades": [],
        "equity": [],
        "rejection_counts": {},
        "rejections": [],
        "events": [],
    }
    defaults.update(overrides)
    return render_report(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------ 自己完結性


def test_外部リソースを参照しない() -> None:
    """CDNやローカルの別ファイルに依存すると、コピーした先で壊れる。"""
    html = _render(trades=[_trade_row("100")], equity=_equity([100, 200]))

    for forbidden in ("http://", "https://", "<script", "src="):
        assert forbidden not in html, f"外部依存が混入しています: {forbidden}"


def test_HTMLとして最低限の体裁がある() -> None:
    html = _render()
    assert html.startswith("<!DOCTYPE html>")
    assert '<html lang="ja">' in html
    assert '<meta charset="utf-8">' in html
    assert html.rstrip().endswith("</html>")


def test_値はHTMLエスケープされる() -> None:
    """ブローカーや戦略が返す文字列がそのまま埋め込まれる箇所がある。"""
    html = _render(events=[EventRow(kind="halt", detail="<script>x</script>", created_at=START)])
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


# ------------------------------------------------------------ 中身


def test_成績が表示される() -> None:
    summary = summarize([_trade_row("100"), _trade_row("-40")])
    html = _render(summary=summary)

    assert "確定損益" in html
    assert "+60" in html
    assert "プロフィットファクタ" in html
    assert "最大ドローダウン" in html


def test_負けが無ければプロフィットファクタはダッシュ表示() -> None:
    html = _render(summary=summarize([_trade_row("100")]))
    assert "—" in html


def test_トレード履歴が表に出る() -> None:
    html = _render(trades=[_trade_row("1234", symbol="EUR_JPY", reason="take_profit")])
    assert "EUR_JPY" in html
    assert "take_profit" in html
    assert "+1,234" in html


def test_トレードが無ければその旨を出す() -> None:
    html = _render()
    assert "まだ決済されたトレードがありません" in html


def test_却下ルールの内訳が出る() -> None:
    html = _render(rejection_counts={"max_risk_per_trade": 3, "atr_spike": 1})
    assert "max_risk_per_trade" in html
    assert "atr_spike" in html


def test_却下が無ければその旨を出す() -> None:
    assert "却下された注文はありません" in _render()


# ------------------------------------------------------------ equity カーブ


def test_equityカーブがSVGとして描かれる() -> None:
    html = _render(equity=_equity([1000, 1100, 1050, 1200]))
    assert "<svg" in html
    assert "<polyline" in html
    assert "viewBox" in html


def test_点が足りなければ線を引かない() -> None:
    """1点だけで線を引くとゼロ除算になる。"""
    html = _render(equity=_equity([1000]))
    assert "<polyline" not in html
    assert "記録が足りません" in html


def test_完全な横ばいでも描画できる() -> None:
    """値の幅がゼロだと正規化でゼロ除算する。"""
    html = _render(equity=_equity([1000, 1000, 1000]))
    assert "<polyline" in html


# ------------------------------------------------------------ ファイル書き出し


def test_記録層からファイルを書き出せる(tmp_path: Path) -> None:
    db = tmp_path / "zerotrade.db"
    with Store(db) as store:
        store.record_trade(
            ClosedTrade(
                symbol="USD_JPY",
                side=Side.BUY,
                quantity=Decimal(10_000),
                entry_price=Decimal("150.00"),
                exit_price=Decimal("151.00"),
                realized_pnl=Decimal(10_000),
                opened_at=START,
                closed_at=START,
                trade_id="t1",
                reason="take_profit",
            )
        )
        store.record_equity(
            Balance(
                currency="JPY",
                equity=Decimal(1_010_000),
                available=Decimal(1_010_000),
                used_margin=Decimal(0),
            )
        )
        output = build_report(store, tmp_path / "out" / "report.html")

    assert output.is_file()
    content = output.read_text(encoding="utf-8")
    assert "ZeroTrade レポート" in content
    assert "take_profit" in content
    assert "全期間" in content


def test_日数を指定すると期間ラベルが変わる(tmp_path: Path) -> None:
    db = tmp_path / "zerotrade.db"
    with Store(db) as store:
        output = build_report(store, tmp_path / "report.html", days=7)
    assert "直近 7 日" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("pnl", ["100", "-100", "0"])
def test_損益の符号で色分けされる(pnl: str) -> None:
    html = _render(trades=[_trade_row(pnl)])
    expected = {"100": "win", "-100": "loss", "0": "muted"}[pnl]
    assert f'class="num {expected}"' in html


# ------------------------------------------------------------ 価格の表示


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("142.90829646077666361063115710", "142.9083"),  # ATR由来の長い小数
        ("150.00", "150"),
        ("149", "149"),
        ("1000", "1000"),
    ],
)
def test_価格は5桁で丸めて末尾ゼロを落とす(raw: str, expected: str) -> None:
    """ATRから逆算したストップは小数28桁まで伸びる。表に出すのは呼値の刻みまで。"""
    assert format_price(Decimal(raw)) == expected


def test_トレード表の価格が丸められている() -> None:
    row = TradeRow(
        symbol="USD_JPY",
        side="buy",
        quantity=Decimal(1000),
        entry_price=Decimal("150.00"),
        exit_price=Decimal("142.90829646077666361063115710"),
        realized_pnl=Decimal(-100),
        opened_at=START,
        closed_at=START,
        reason="stop_loss",
        strategy="sma_rsi",
    )
    html = _render(trades=[row])
    assert "142.9083" in html
    assert "142.90829646" not in html
