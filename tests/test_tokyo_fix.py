"""仲値戦略のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import make_position
from zerotrade.errors import ConfigError
from zerotrade.models import Candle, Side, SignalAction
from zerotrade.strategies import create_strategy
from zerotrade.strategies.base import StrategyContext
from zerotrade.strategies.tokyo_fix import TokyoFixStrategy

JST = ZoneInfo("Asia/Tokyo")


def _bars_until(hour: int, minute: int, *, count: int = 30) -> list[Candle]:
    """JSTの指定時刻で終わる5分足の列を作る。"""
    end = datetime(2024, 4, 3, hour, minute, tzinfo=JST).astimezone(UTC)
    return [
        Candle(
            symbol="USD_JPY",
            timestamp=end - timedelta(minutes=5 * (count - 1 - i)),
            open=Decimal(150),
            high=Decimal("150.3"),
            low=Decimal("149.7"),
            close=Decimal(150) + Decimal(i) / 100,
        )
        for i in range(count)
    ]


def _ctx(hour: int, minute: int, **kw: object) -> StrategyContext:
    return StrategyContext(symbol="USD_JPY", candles=_bars_until(hour, minute), **kw)  # type: ignore[arg-type]


def test_レジストリに登録されている() -> None:
    assert isinstance(create_strategy("tokyo_fix"), TokyoFixStrategy)


@pytest.mark.parametrize(
    "params",
    [
        {"entry_time": "こわれた"},
        {"entry_time": "10:00", "exit_time": "09:00"},
        {"timezone": "Asia/Nowhere"},
        {"side": "maybe"},
        {"存在しない": 1},
    ],
)
def test_不正なパラメータは拒否される(params: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="パラメータが不正"):
        create_strategy("tokyo_fix", params)


def test_開始時刻の足でエントリーする() -> None:
    signal = TokyoFixStrategy(atr_period=5).generate(_ctx(8, 0))

    assert signal.action is SignalAction.ENTER_LONG
    assert signal.stop_loss is not None
    assert signal.take_profit is None
    assert "仲値" in signal.reason


def test_窓の外では入らない() -> None:
    for hour, minute in ((7, 55), (10, 30), (15, 0)):
        signal = TokyoFixStrategy(atr_period=5).generate(_ctx(hour, minute))
        assert signal.action is SignalAction.HOLD, f"{hour}:{minute} で入っている"


def test_窓の途中では追撃しない() -> None:
    """開始をまたいだ最初の足だけで入る。毎足入ると建玉が積み上がる。"""
    signal = TokyoFixStrategy(atr_period=5).generate(_ctx(8, 30))
    assert signal.action is SignalAction.HOLD


def test_仲値時刻を過ぎたら決済する() -> None:
    position = make_position(side=Side.BUY)
    signal = TokyoFixStrategy(atr_period=5).generate(_ctx(9, 55, position=position))
    assert signal.action is SignalAction.EXIT
    assert "09:55" in signal.reason


def test_窓の中では保有を続ける() -> None:
    position = make_position(side=Side.BUY)
    signal = TokyoFixStrategy(atr_period=5).generate(_ctx(9, 0, position=position))
    assert signal.action is SignalAction.HOLD


def test_売り方向も指定できる() -> None:
    signal = TokyoFixStrategy(atr_period=5, side="sell").generate(_ctx(8, 0))
    assert signal.action is SignalAction.ENTER_SHORT
    assert signal.stop_loss is not None
    assert signal.stop_loss > Decimal(150)


def test_時刻の判定はタイムゾーンに従う() -> None:
    """UTCで解釈すると9時間ずれ、まったく別の時間帯を取引してしまう。"""
    jst = TokyoFixStrategy(atr_period=5).generate(_ctx(8, 0))
    utc = TokyoFixStrategy(atr_period=5, timezone="UTC").generate(_ctx(8, 0))

    assert jst.action is SignalAction.ENTER_LONG
    assert utc.action is SignalAction.HOLD


def test_ウォームアップ中はHOLD() -> None:
    ctx = StrategyContext(symbol="USD_JPY", candles=_bars_until(8, 0, count=3))
    assert TokyoFixStrategy(atr_period=14).generate(ctx).action is SignalAction.HOLD
