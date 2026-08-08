"""ドンチャン戦略とトレーリングストップのテスト。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import START, make_position
from zerotrade.errors import ConfigError
from zerotrade.models import Candle, Side, SignalAction
from zerotrade.strategies import create_strategy
from zerotrade.strategies.base import StrategyContext
from zerotrade.strategies.donchian import DonchianStrategy


def _candles(closes: list[float], *, spread: float = 0.2) -> list[Candle]:
    return [
        Candle(
            symbol="USD_JPY",
            timestamp=START + timedelta(hours=i),
            open=Decimal(str(c)),
            high=Decimal(str(c + spread)),
            low=Decimal(str(c - spread)),
            close=Decimal(str(c)),
        )
        for i, c in enumerate(closes)
    ]


def _ctx(closes: list[float], **kw: object) -> StrategyContext:
    return StrategyContext(symbol="USD_JPY", candles=_candles(closes), **kw)  # type: ignore[arg-type]


def _strategy(**kw: object) -> DonchianStrategy:
    params: dict[str, object] = {
        "entry_period": 10,
        "exit_period": 5,
        "trend_period": 20,
        "atr_period": 5,
    }
    params.update(kw)
    return DonchianStrategy(**params)  # type: ignore[arg-type]


# ------------------------------------------------------------ 登録・検証


def test_レジストリに登録されている() -> None:
    assert isinstance(create_strategy("donchian"), DonchianStrategy)


def test_不正なパラメータは拒否される() -> None:
    with pytest.raises(ConfigError, match="パラメータが不正"):
        create_strategy("donchian", {"entry_period": 1})
    with pytest.raises(ConfigError, match="パラメータが不正"):
        create_strategy("donchian", {"存在しない": 1})


def test_ウォームアップ中はHOLD() -> None:
    signal = _strategy().generate(_ctx([150.0] * 5))
    assert signal.action is SignalAction.HOLD
    assert "ウォームアップ" in signal.reason


# ------------------------------------------------------------ エントリー


def test_高値ブレイクでロング() -> None:
    # 上昇基調で最後にブレイクさせる（長期トレンドフィルタも通す）
    closes = [150 + i * 0.1 for i in range(30)] + [154.0]
    signal = _strategy().generate(_ctx(closes))

    assert signal.action is SignalAction.ENTER_LONG
    assert signal.stop_loss is not None
    assert signal.stop_loss < Decimal("154.0")
    assert signal.take_profit is None, "順張りは利確を置かない（伸びる余地を残す）"


def test_安値ブレイクでショート() -> None:
    closes = [160 - i * 0.1 for i in range(30)] + [156.0]
    signal = _strategy().generate(_ctx(closes))

    assert signal.action is SignalAction.ENTER_SHORT
    assert signal.stop_loss is not None
    assert signal.stop_loss > Decimal("156.0")


def _downtrend_then_bounce() -> list[float]:
    """急落 → 横ばい → 小さな戻り。

    直線的な下降だけでは10本高値を抜いた時点で長期平均も超えてしまう。
    「下降のあと保ち合い、その上限を少し抜く」という現実的な形にする。
    """
    return [200 - i * 2 for i in range(20)] + [160.0] * 15 + [161.0]


def test_長期トレンドに逆らうブレイクは見送る() -> None:
    """下降基調の中の高値ブレイクは、ダマシになりやすい。"""
    signal = _strategy(trend_period=30, use_trend_filter=True).generate(
        _ctx(_downtrend_then_bounce())
    )
    assert signal.action is SignalAction.HOLD
    assert "長期トレンド" in signal.reason


def test_フィルタを切れば同じ場面で入る() -> None:
    signal = _strategy(trend_period=30, use_trend_filter=False).generate(
        _ctx(_downtrend_then_bounce())
    )
    assert signal.action is SignalAction.ENTER_LONG


def test_ショート無効なら安値ブレイクを見送る() -> None:
    closes = [160 - i * 0.1 for i in range(30)] + [156.0]
    signal = _strategy(allow_short=False).generate(_ctx(closes))
    assert signal.action is SignalAction.HOLD
    assert "ショートは無効" in signal.reason


def test_ブレイクが無ければHOLD() -> None:
    signal = _strategy().generate(_ctx([150.0] * 30))
    assert signal.action is SignalAction.HOLD


# ------------------------------------------------------------ 時間帯フィルタ


def test_時間帯の外では入らない() -> None:
    closes = [150 + i * 0.1 for i in range(30)] + [154.0]
    ctx = _ctx(closes)
    hour = ctx.candles[-1].timestamp.hour
    # 最終足の時刻を確実に外す帯を指定する
    outside = (hour + 5) % 24
    signal = _strategy(min_hour_utc=outside, max_hour_utc=outside).generate(ctx)
    assert signal.action is SignalAction.HOLD
    assert "時間帯" in signal.reason


def test_日をまたぐ時間帯指定も効く() -> None:
    closes = [150 + i * 0.1 for i in range(30)] + [154.0]
    ctx = _ctx(closes)
    hour = ctx.candles[-1].timestamp.hour
    # hour を含む「22時〜翌6時」型の指定
    signal = _strategy(min_hour_utc=hour, max_hour_utc=(hour + 2) % 24).generate(ctx)
    assert signal.action is SignalAction.ENTER_LONG


# ------------------------------------------------------------ 保有中


def test_逆方向のブレイクで決済する() -> None:
    closes = [150 + i * 0.1 for i in range(30)] + [148.0]
    position = make_position(side=Side.BUY, entry_price=Decimal("153"))
    signal = _strategy().generate(_ctx(closes, position=position))
    assert signal.action is SignalAction.EXIT


def test_利益が伸びればストップを引き上げる() -> None:
    closes = [150 + i * 0.3 for i in range(40)]
    position = make_position(side=Side.BUY, entry_price=Decimal("150"), stop_loss=Decimal("149"))
    signal = _strategy().generate(_ctx(closes, position=position))

    assert signal.action is SignalAction.UPDATE_STOP
    assert signal.stop_loss is not None
    assert signal.stop_loss > Decimal("149"), "ストップが引き上がっていない"


def test_ストップは引き下げない() -> None:
    """既に十分高いストップがあれば、下げる提案はしない。"""
    closes = [150 + i * 0.3 for i in range(40)]
    position = make_position(side=Side.BUY, entry_price=Decimal("150"), stop_loss=Decimal("161"))
    signal = _strategy().generate(_ctx(closes, position=position))
    assert signal.action is SignalAction.HOLD
