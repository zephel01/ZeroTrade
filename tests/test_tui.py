"""TUI ダッシュボードのテスト。

Textual の Pilot で実際に起動し、画面が組み上がることと
緊急停止が2回押しで確定することを確認する。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import START
from zerotrade.control import KillSwitch
from zerotrade.models import Balance, ClosedTrade, Side
from zerotrade.settings import Settings
from zerotrade.store import Store
from zerotrade.tui import _build_app, _read_risk_state

pytest.importorskip("textual", reason="TUI は任意依存（zerotrade[ui]）")


def _text(app: object, selector: str) -> str:
    """Static ウィジェットの表示テキスト（マークアップ込み）を取り出す。"""
    widget = app.query_one(selector)  # type: ignore[attr-defined]
    return str(widget.content)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "mode": "paper",
            "symbols": ["USD_JPY"],
            "state_dir": str(tmp_path),
            "notifications": {"console": False},
        }
    )


def _seed(settings: Settings) -> None:
    with Store(settings.database_path) as store:
        store.record_trade(
            ClosedTrade(
                symbol="USD_JPY",
                side=Side.BUY,
                quantity=Decimal(10_000),
                entry_price=Decimal("150.00"),
                exit_price=Decimal("149.00"),
                realized_pnl=Decimal(-10_000),
                opened_at=START,
                closed_at=START,
                trade_id="t1",
                reason="stop_loss",
            )
        )
        store.record_equity(
            Balance(
                currency="JPY",
                equity=Decimal(990_000),
                available=Decimal(990_000),
                used_margin=Decimal(0),
            ),
            open_positions=1,
        )
        store.record_event("start", "テスト")


# ------------------------------------------------------------ リスク状態の読み取り


def test_リスク状態ファイルが無ければ空(tmp_path: Path) -> None:
    assert _read_risk_state(tmp_path) == {}


def test_壊れたリスク状態ファイルでも落ちない(tmp_path: Path) -> None:
    (tmp_path / "risk_state.json").write_text("{壊れている", encoding="utf-8")
    assert _read_risk_state(tmp_path) == {}


def test_リスク状態を読める(tmp_path: Path) -> None:
    (tmp_path / "risk_state.json").write_text(
        json.dumps({"halt_reason": "daily_loss_limit"}), encoding="utf-8"
    )
    assert _read_risk_state(tmp_path)["halt_reason"] == "daily_loss_limit"


# ------------------------------------------------------------ 画面


async def test_記録が無くても起動できる(settings: Settings) -> None:
    """`zerotrade run` の前にダッシュボードを開いても落ちないこと。"""
    app = _build_app(settings)()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "記録がまだありません" in _text(app, "#status")


async def test_記録があれば表に出る(settings: Settings) -> None:
    _seed(settings)
    app = _build_app(settings)()
    async with app.run_test() as pilot:
        await pilot.pause()
        trades = app.query_one("#trades")
        assert trades.row_count == 1
        events = app.query_one("#events")
        assert events.row_count == 1


async def test_停止中の状態が表示される(settings: Settings) -> None:
    _seed(settings)
    (settings.state_dir / "risk_state.json").write_text(
        json.dumps({"halt_reason": "daily_loss_limit"}), encoding="utf-8"
    )
    app = _build_app(settings)()
    async with app.run_test() as pilot:
        await pilot.pause()
        tile = _text(app, "#tile-state")
        assert "停止中" in tile
        assert "daily_loss_limit" in tile


# ------------------------------------------------------------ 緊急停止


async def test_緊急停止は2回押しで確定する(settings: Settings) -> None:
    """誤爆で取引を止めないための確認。1回目では要求が作られない。"""
    _seed(settings)
    switch = KillSwitch(settings.state_dir)
    app = _build_app(settings)()

    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()
        assert switch.requested() is None, "1回押しで停止してはいけない"
        assert "もう一度" in _text(app, "#status")

        await pilot.press("s")
        await pilot.pause()
        assert switch.requested() == "ダッシュボードからの緊急停止"


async def test_更新キーで再読み込みされる(settings: Settings) -> None:
    app = _build_app(settings)()
    async with app.run_test() as pilot:
        await pilot.pause()
        _seed(settings)  # 起動後に記録が増えた状況を作る

        await pilot.press("r")
        await pilot.pause()
        assert app.query_one("#trades").row_count == 1
