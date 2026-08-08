"""RiskManager のテスト。

このファイルがシステムで最も重要なテスト。
ここが通らない限り、実弾を入れてはいけない。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import FakeClock, make_position, make_request
from zerotrade.core.risk import MarketContext, RiskManager, RiskState
from zerotrade.errors import RiskViolation, TradingHalted
from zerotrade.models import Balance, Side, Ticker
from zerotrade.settings import RiskSettings


def halted(manager: RiskManager) -> bool:
    """取引が停止しているか。

    ``manager.is_halted`` を同じ関数内で2回 assert すると、型チェッカが
    プロパティの値を絞り込んで以降を到達不能と判断してしまう。
    関数越しに読むことでその誤検知を避ける。
    """
    return manager.is_halted


# ------------------------------------------------------------ 基本の承認


def test_通常の注文は承認される(risk: RiskManager, balance: Balance, ticker: Ticker) -> None:
    decision = risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert decision.approved
    assert decision.rule is None


# ------------------------------------------------------- 1トレードリスク上限


def test_リスクが上限を超える注文は却下される(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    # ストップまで1.00円 × 20000通貨 = 20,000円 > equity 100万の1% (10,000円)
    request = make_request(quantity=Decimal(20_000), stop_loss=Decimal("149.00"))
    decision = risk.evaluate(
        request, balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "max_risk_per_trade"


def test_リスク上限ちょうどは承認される(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    # 参照価格は ask(150.02)。ストップ 149.02 で距離ちょうど 1.00 円。
    request = make_request(quantity=Decimal(10_000), stop_loss=Decimal("149.02"))
    decision = risk.evaluate(
        request, balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert decision.approved, decision.detail


# ------------------------------------------------------------ ストップ必須


def test_ストップ無しの新規は却下される(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    decision = risk.evaluate(
        make_request(stop_loss=None),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    assert not decision.approved
    assert decision.rule == "require_stop_loss"


@pytest.mark.parametrize(
    ("side", "stop_loss"),
    [(Side.BUY, Decimal("151.00")), (Side.SELL, Decimal("149.00"))],
)
def test_ストップが反対側にある注文は却下される(
    risk: RiskManager, balance: Balance, ticker: Ticker, side: Side, stop_loss: Decimal
) -> None:
    decision = risk.evaluate(
        make_request(side=side, stop_loss=stop_loss),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    assert not decision.approved
    assert decision.rule == "invalid_stop_loss"


def test_参照価格が無ければ却下される(risk: RiskManager, balance: Balance) -> None:
    decision = risk.evaluate(make_request(), balance=balance, positions=[])
    assert not decision.approved
    assert decision.rule == "no_reference_price"


# ------------------------------------------------------------ ポジション数


def test_同時保有数の上限で却下される(risk: RiskManager, balance: Balance, ticker: Ticker) -> None:
    positions = [make_position(symbol=s) for s in ("EUR_USD", "GBP_JPY", "AUD_JPY")]
    decision = risk.evaluate(
        make_request(), balance=balance, positions=positions, market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "max_open_positions"


def test_同一銘柄の重複保有は却下される(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    decision = risk.evaluate(
        make_request(),
        balance=balance,
        positions=[make_position(symbol="USD_JPY")],
        market=MarketContext(ticker=ticker),
    )
    assert not decision.approved
    assert decision.rule == "max_positions_per_symbol"


# ------------------------------------------------------------ 証拠金


def test_証拠金使用率の上限で却下される(risk: RiskManager, ticker: Ticker) -> None:
    # 150.02 × 100,000 / 25 = 600,080円 の証拠金 → equity の 60% で上限30%超え
    balance = Balance(
        currency="JPY",
        equity=Decimal(1_000_000),
        available=Decimal(1_000_000),
        used_margin=Decimal(0),
    )
    request = make_request(quantity=Decimal(100_000), stop_loss=Decimal("150.00"))
    decision = risk.evaluate(
        request, balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "max_margin_usage"


def test_equityがゼロなら却下される(risk: RiskManager, ticker: Ticker) -> None:
    balance = Balance(
        currency="JPY", equity=Decimal(0), available=Decimal(0), used_margin=Decimal(0)
    )
    decision = risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "equity_depleted"


# ------------------------------------------------------------ 損失上限と停止


def test_日次損失上限に達すると停止する(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    assert not halted(risk)
    risk.record_trade_closed("USD_JPY", Decimal(-30_000))  # equity 100万の 3%

    assert halted(risk)
    decision = risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "daily_loss_limit"


def test_日次上限に触れない小さな負けの積み重ねでも週次で止まる(
    risk: RiskManager, clock: FakeClock
) -> None:
    """1日あたり2.5%（日次上限3%未満）の負けを重ね、週次6%で停止することを確認する。

    日次上限しか見ていないと、じわじわ溶けるパターンを止められない。
    """
    risk.record_trade_closed("USD_JPY", Decimal(-25_000))  # 週次 2.5%
    assert risk.state.halt_reason is None

    clock.advance(days=1)
    risk.record_trade_closed("USD_JPY", Decimal(-25_000))  # 週次 5.0%
    assert risk.state.halt_reason is None

    clock.advance(days=1)
    risk.record_trade_closed("USD_JPY", Decimal(-11_000))  # 週次 6.1%
    assert risk.state.halt_reason == "weekly_loss_limit"


def test_停止中でも決済注文は通る(risk: RiskManager, balance: Balance, ticker: Ticker) -> None:
    risk.record_trade_closed("USD_JPY", Decimal(-50_000))
    assert halted(risk)

    decision = risk.evaluate(
        make_request(stop_loss=None, reduce_only=True),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    assert decision.approved, "停止中に建玉を閉じられないのは致命的"


def test_日付が変われば日次停止は解除される(
    risk: RiskManager, clock: FakeClock, balance: Balance, ticker: Ticker
) -> None:
    risk.record_trade_closed("USD_JPY", Decimal(-30_000))
    assert halted(risk)

    clock.advance(days=1)
    assert not halted(risk)
    assert risk.state.daily_pnl == 0
    # 週次損益は持ち越される。
    assert risk.state.weekly_pnl == Decimal(-30_000)

    decision = risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert decision.approved


def test_週次停止は日付が変わっても解除されない(risk: RiskManager, clock: FakeClock) -> None:
    risk.record_trade_closed("USD_JPY", Decimal(-60_000))
    assert risk.state.halt_reason == "weekly_loss_limit"

    clock.advance(days=1)
    assert halted(risk), "週次停止が日次リセットで消えてはいけない"

    clock.advance(days=7)
    assert not halted(risk)


# ------------------------------------------------------------ 取引回数


def test_取引回数の上限で却下される(
    risk_settings: RiskSettings, clock: FakeClock, balance: Balance, ticker: Ticker
) -> None:
    settings = risk_settings.model_copy(update={"max_daily_trades": 2})
    manager = RiskManager(settings, clock=clock)

    for _ in range(2):
        request = make_request()
        assert manager.evaluate(
            request, balance=balance, positions=[], market=MarketContext(ticker=ticker)
        ).approved
        manager.record_order_submitted(request)

    decision = manager.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "max_daily_trades"


def test_決済注文は取引回数に数えない(risk: RiskManager) -> None:
    risk.record_order_submitted(make_request(reduce_only=True, stop_loss=None))
    assert risk.state.daily_trades == 0


# ------------------------------------------------------------ 相場状況


def test_ATR急拡大時は新規を止める(risk: RiskManager, balance: Balance, ticker: Ticker) -> None:
    market = MarketContext(ticker=ticker, atr=Decimal("0.40"), atr_baseline=Decimal("0.10"))
    decision = risk.evaluate(make_request(), balance=balance, positions=[], market=market)
    assert not decision.approved
    assert decision.rule == "atr_spike"


def test_ATRが平常範囲なら通す(risk: RiskManager, balance: Balance, ticker: Ticker) -> None:
    market = MarketContext(ticker=ticker, atr=Decimal("0.12"), atr_baseline=Decimal("0.10"))
    assert risk.evaluate(make_request(), balance=balance, positions=[], market=market).approved


def test_スプレッド超過で却下される(
    risk_settings: RiskSettings, clock: FakeClock, balance: Balance
) -> None:
    settings = risk_settings.model_copy(update={"max_spread": Decimal("0.03")})
    manager = RiskManager(settings, clock=clock)
    wide = Ticker(symbol="USD_JPY", bid=Decimal("150.00"), ask=Decimal("150.10"))

    decision = manager.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=wide)
    )
    assert not decision.approved
    assert decision.rule == "max_spread"


def test_損切り後クールダウン中は却下される(
    risk_settings: RiskSettings, clock: FakeClock, balance: Balance, ticker: Ticker
) -> None:
    settings = risk_settings.model_copy(update={"cooldown_seconds_after_loss": 600})
    manager = RiskManager(settings, clock=clock)
    manager.set_reference_equity(Decimal(1_000_000))
    manager.record_trade_closed("USD_JPY", Decimal(-1_000))

    decision = manager.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert not decision.approved
    assert decision.rule == "cooldown_after_loss"

    clock.advance(seconds=601)
    assert manager.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    ).approved


def test_クールダウンは銘柄ごとに独立している(
    risk_settings: RiskSettings, clock: FakeClock, balance: Balance
) -> None:
    settings = risk_settings.model_copy(update={"cooldown_seconds_after_loss": 600})
    manager = RiskManager(settings, clock=clock)
    manager.set_reference_equity(Decimal(1_000_000))
    manager.record_trade_closed("USD_JPY", Decimal(-1_000))

    other = Ticker(symbol="EUR_JPY", bid=Decimal("160.00"), ask=Decimal("160.02"))
    decision = manager.evaluate(
        make_request(symbol="EUR_JPY", stop_loss=Decimal("159.00")),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=other),
    )
    assert decision.approved


# ------------------------------------------------------------ 例外への変換


def test_raise_if_rejectedがルール別の例外を投げる(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    decision = risk.evaluate(
        make_request(stop_loss=None),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    with pytest.raises(RiskViolation) as info:
        decision.raise_if_rejected()
    assert info.value.rule == "require_stop_loss"

    risk.record_trade_closed("USD_JPY", Decimal(-60_000))
    halted = risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    with pytest.raises(TradingHalted):
        halted.raise_if_rejected()


def test_承認された判定はraiseしない(risk: RiskManager, balance: Balance, ticker: Ticker) -> None:
    decision = risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    decision.raise_if_rejected()  # 例外が出なければ成功


# ------------------------------------------------------------ 永続化


def test_状態はファイルへ保存され復元される(
    risk_settings: RiskSettings, clock: FakeClock, tmp_path: Path
) -> None:
    path = tmp_path / "risk_state.json"
    manager = RiskManager(risk_settings, state_path=path, clock=clock)
    manager.set_reference_equity(Decimal(1_000_000))
    manager.record_trade_closed("USD_JPY", Decimal(-30_000))
    assert halted(manager)

    restored = RiskManager.load(risk_settings, path, clock=clock)
    assert restored.is_halted, "再起動で損失上限がリセットされてはいけない"
    assert restored.state.daily_pnl == Decimal(-30_000)


def test_壊れた状態ファイルは初期状態として扱う(
    risk_settings: RiskSettings, clock: FakeClock, tmp_path: Path
) -> None:
    path = tmp_path / "risk_state.json"
    path.write_text("{ broken json", encoding="utf-8")

    manager = RiskManager.load(risk_settings, path, clock=clock)
    assert not halted(manager)
    assert manager.state.daily_pnl == 0


def test_週境界でカウンタがリセットされる(clock: FakeClock) -> None:
    state = RiskState()
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    state.roll(clock.now, tz)
    state.weekly_pnl = Decimal(-1_000)
    first_week = state.week_key

    clock.advance(days=7)
    state.roll(clock.now, tz)
    assert state.week_key != first_week
    assert state.weekly_pnl == 0
