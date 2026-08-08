"""設定ローダのテスト。

「起動時に落ちる方が、リスク上限が壊れたまま動き出すより安全」
という方針が守られているかを確認する。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from zerotrade.errors import ConfigError
from zerotrade.settings import RiskSettings, Settings, load_settings

MINIMAL = """
mode: paper
symbols: [USD_JPY]
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_既定値だけで構築できる() -> None:
    settings = load_settings()
    assert settings.mode == "paper"
    assert settings.risk.require_stop_loss is True


def test_YAMLから読み込める(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, MINIMAL))
    assert settings.symbols == ["USD_JPY"]


def test_存在しないファイルはConfigError(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="見つかりません"):
        load_settings(tmp_path / "nope.yaml")


def test_壊れたYAMLはConfigError(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="パース"):
        load_settings(_write(tmp_path, "mode: [unclosed"))


def test_未知のキーは拒否される(tmp_path: Path) -> None:
    """typo を黙って無視すると、意図した設定が効かないまま動いてしまう。"""
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, "mode: paper\nmax_risk: 0.5\n"))


def test_環境変数が展開される(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_TOKEN", "secret-value")
    settings = load_settings(
        _write(tmp_path, "broker:\n  name: paper\n  api_token: ${TEST_TOKEN}\n")
    )
    assert settings.broker.api_token == "secret-value"


def test_未定義の環境変数はNoneになる(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空文字にすると「トークン設定済み」と誤認され、実接続を試みてしまう。"""
    monkeypatch.delenv("TEST_MISSING", raising=False)
    settings = load_settings(
        _write(tmp_path, "broker:\n  name: paper\n  api_token: ${TEST_MISSING}\n")
    )
    assert settings.broker.api_token is None


def test_環境変数のデフォルト値(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_ENV", raising=False)
    settings = load_settings(
        _write(tmp_path, "broker:\n  name: paper\n  account_id: ${TEST_ENV:-fallback}\n")
    )
    assert settings.broker.account_id == "fallback"


def test_overridesがYAMLより優先される(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, MINIMAL), overrides={"mode": "backtest"})
    assert settings.mode == "backtest"


# ------------------------------------------------------- 危険な設定を弾く


def test_リスク率が過大なら拒否される() -> None:
    with pytest.raises(ValueError):
        RiskSettings(max_risk_per_trade=Decimal("0.5"))


def test_リスク率がゼロなら拒否される() -> None:
    with pytest.raises(ValueError):
        RiskSettings(max_risk_per_trade=Decimal(0))


def test_週次上限が日次未満なら拒否される() -> None:
    with pytest.raises(ValueError, match="max_weekly_loss"):
        RiskSettings(max_daily_loss=Decimal("0.05"), max_weekly_loss=Decimal("0.02"))


def test_1トレードリスクが日次上限を超えたら拒否される() -> None:
    with pytest.raises(ValueError, match="max_risk_per_trade"):
        RiskSettings(max_risk_per_trade=Decimal("0.05"), max_daily_loss=Decimal("0.02"))


def test_未知のタイムゾーンは拒否される() -> None:
    with pytest.raises(ValueError, match="タイムゾーン"):
        RiskSettings(reset_timezone="Asia/Nowhere")


def test_liveモードでpaperブローカーは拒否される(tmp_path: Path) -> None:
    """実運用のつもりが疑似約定だった、という事故を構造的に防ぐ。"""
    with pytest.raises(ConfigError, match="live"):
        load_settings(_write(tmp_path, "mode: live\nbroker:\n  name: paper\n"))


def test_銘柄の重複は拒否される() -> None:
    with pytest.raises(ValueError, match="重複"):
        Settings(symbols=["USD_JPY", "USD_JPY"])


def test_銘柄が空なら拒否される() -> None:
    with pytest.raises(ValueError, match="1つ以上"):
        Settings(symbols=[])


def test_同梱の設定ファイルは有効() -> None:
    """リポジトリに置いてあるサンプル設定が壊れていないことを保証する。"""
    root = Path(__file__).resolve().parents[1]
    for name in ("paper.yaml", "oanda.yaml"):
        settings = load_settings(root / "config" / name)
        assert settings.risk.require_stop_loss is True


def test_足種は検証と揃える必要がある() -> None:
    """既定(M5)のまま H1 の戦略を回すと、検証結果が保証にならない。"""
    from zerotrade.settings import StrategySettings

    assert StrategySettings().granularity == "M5"
    assert StrategySettings(granularity="h1").granularity == "H1"


def test_未知の足種は拒否される() -> None:
    from zerotrade.settings import StrategySettings

    with pytest.raises(ValidationError, match="未知の足種"):
        StrategySettings(granularity="H3")


def test_modeがpaperなら本番ブローカーを拒否する() -> None:
    """`mode: paper` と書いてあるのに実弾が飛ぶのが、最悪の裏切り方。"""
    from zerotrade.app import build_application

    settings = Settings.model_validate(
        {
            "mode": "paper",
            "symbols": ["BTC_USDT"],
            "broker": {
                "name": "bingx",
                "environment": "live",
                "api_token": "k",
                "api_secret": "s",
                "fallback_to_paper": False,
            },
        }
    )
    with pytest.raises(ConfigError, match="本物の注文"):
        build_application(settings)


def test_テストネットならpaperのままでよい() -> None:
    """VST は実弾が動かないので、mode を live にする必要はない。"""
    from zerotrade.brokers import create_broker

    settings = Settings.model_validate(
        {
            "mode": "paper",
            "symbols": ["BTC_USDT"],
            "broker": {
                "name": "bingx",
                "environment": "practice",
                "api_token": "k",
                "api_secret": "s",
            },
        }
    )
    broker = create_broker(settings)
    assert broker.is_simulated is False, "実ブローカーのはず"
    # environment=practice なので実弾ガードは発動しない
    from zerotrade.app import _check_live_guard

    _check_live_guard(settings, broker)


def test_シャドーは本番環境でも許される() -> None:
    """価格だけ本番を読む。注文は外へ出ないため mode: paper のままでよい。"""
    from zerotrade.app import _check_live_guard
    from zerotrade.brokers.paper import PaperBroker

    settings = Settings.model_validate(
        {"mode": "paper", "symbols": ["BTC_USDT"], "broker": {"environment": "live"}}
    )
    broker = PaperBroker(["BTC_USDT"])
    assert broker.is_simulated is True
    _check_live_guard(settings, broker)
