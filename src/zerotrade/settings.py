"""YAML + 環境変数による設定。

設計方針:

* すべての設定は pydantic で検証する。起動時に落ちる方が、
  リスク上限がゼロのまま動き出すより遥かに安全。
* APIキーなどの秘密情報は YAML に直接書かず ``${OANDA_API_TOKEN}`` の形で
  環境変数を参照する。未定義ならその値は ``None`` になる。
* 危険な設定（`max_risk_per_trade` が極端に大きい等）は
  バリデータで上限を設けて弾く。
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zerotrade.errors import ConfigError

__all__ = [
    "BrokerSettings",
    "LoggingSettings",
    "NotificationSettings",
    "RiskSettings",
    "Settings",
    "SizingSettings",
    "StoreSettings",
    "StrategySettings",
    "load_settings",
]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

TradingMode = Literal["paper", "live", "backtest"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BrokerSettings(_Base):
    """ブローカー接続設定。"""

    name: str = "paper"
    """``paper`` / ``oanda`` など。BROKER_REGISTRY のキー。"""

    environment: Literal["practice", "live"] = "practice"
    """``practice`` は ccxt ではテストネット（``set_sandbox_mode``）を意味する。"""

    account_id: str | None = None
    api_token: str | None = None
    """API キー。ccxt では ``apiKey`` に渡る。"""

    api_secret: str | None = None
    """API シークレット。ccxt を使う取引所で必要。"""

    api_passphrase: str | None = None
    """一部の取引所（OKX など）が要求するパスフレーズ。"""

    exchange: str | None = None
    """ccxt の取引所ID（``binance`` / ``bybit`` / ``bitflyer`` など）。"""

    upstream: str | None = None
    """``name: shadow`` のとき、実勢価格の取得元にするブローカー名。

    シャドーブローカーは上流を**読み取りにしか使わない**（発注は手元で模擬）。
    そのため ``environment: live`` を指定しても実弾は動かない。
    """

    market_type: str = "swap"
    """ccxt の ``defaultType``。``spot`` / ``swap`` / ``future``。"""

    margin_mode: Literal["cross", "isolated"] | None = "cross"
    """証拠金モード。``None`` なら取引所の設定に触らない。"""

    base_url: str | None = None
    """明示指定した場合のみ既定のエンドポイントを上書きする。"""

    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    initial_balance: Decimal = Field(default=Decimal(1_000_000), gt=0)
    """PaperBroker 用の初期残高。"""

    account_currency: str = "JPY"
    spread: Decimal = Field(default=Decimal("0.003"), ge=0)
    """PaperBroker とバックテストが付けるスプレッド（価格単位）。

    **実際に使うブローカーの実勢に合わせること。** 短期戦略ではこの値が
    成績を支配する。USD/JPY を5分足で回した実測では、0.3銭なら -8%、
    2銭なら -48% と6倍近い差が出た。既定値は OANDA証券の
    USD/JPY 実勢（およそ0.3銭）に寄せてある。
    """

    fallback_to_paper: bool = True
    """認証情報が無い場合に PaperBroker で起動するか。"""


class RiskSettings(_Base):
    """RiskManager が強制するルール。すべて口座 equity に対する比率。"""

    max_risk_per_trade: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.1"))
    """1トレードで許容する最大損失（ストップまでの距離 × 数量）。"""

    max_daily_loss: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal("0.5"))
    max_weekly_loss: Decimal = Field(default=Decimal("0.06"), gt=0, le=Decimal("0.8"))

    max_margin_usage: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    """証拠金使用率の上限。"""

    max_open_positions: int = Field(default=3, ge=1, le=100)
    max_positions_per_symbol: int = Field(default=1, ge=1, le=100)

    max_daily_trades: int = Field(default=20, ge=1, le=1000)
    """過剰取引の抑制。"""

    require_stop_loss: bool = True
    """ストップ無しの新規建てを禁止する。既定で有効。"""

    atr_spike_multiplier: Decimal = Field(default=Decimal("3.0"), gt=1, le=20)
    """直近ATRが基準ATRのこの倍数を超えたら大相場とみなし新規を止める。"""

    max_spread: Decimal | None = None
    """スプレッドがこの値を超えたら発注しない（価格単位）。None で無効。"""

    cooldown_seconds_after_loss: int = Field(default=0, ge=0, le=86_400)
    """損失確定後、同一シンボルの新規を止める秒数。"""

    assumed_leverage: Decimal = Field(default=Decimal(25), gt=0, le=1000)
    """必要証拠金の見積りに使うレバレッジ。想定元本 ÷ この値を証拠金とみなす。"""

    reset_timezone: str = "UTC"
    """日次・週次カウンタをリセットする基準タイムゾーン（例 ``Asia/Tokyo``）。"""

    @field_validator("reset_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知のタイムゾーンです: {value}") from exc
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> RiskSettings:
        if self.max_weekly_loss < self.max_daily_loss:
            raise ValueError("max_weekly_loss は max_daily_loss 以上にしてください")
        if self.max_risk_per_trade > self.max_daily_loss:
            raise ValueError("max_risk_per_trade は max_daily_loss 以下にしてください")
        return self


class SizingSettings(_Base):
    """PositionSizer の設定。"""

    method: Literal["fixed_fractional", "fixed_quantity"] = "fixed_fractional"
    fixed_quantity: Decimal = Field(default=Decimal(1000), gt=0)
    """``fixed_quantity`` 選択時に使う固定ロット。"""

    min_quantity: Decimal = Field(default=Decimal(1), gt=0)
    max_quantity: Decimal | None = None
    quantity_step: Decimal = Field(default=Decimal(1), gt=0)
    """発注単位。切り捨てで丸める（切り上げるとリスク上限を超えるため）。"""

    contract_size: Decimal = Field(default=Decimal(1), gt=0)
    """1単位あたりの想定元本倍率。FXの通貨単位建てなら 1。"""


class StrategySettings(_Base):
    name: str = "sma_rsi"
    params: dict[str, Any] = Field(default_factory=dict)

    granularity: str = "M5"
    """戦略が見る足種（``M1`` / ``M5`` / ``M15`` / ``M30`` / ``H1`` / ``H4`` / ``D1``）。

    **バックテストで使った足種と必ず揃えること。** ここが違えば、同じ
    パラメータでもまったく別の戦略になる。H1 で検証した設定を既定の M5 で
    走らせると、検証結果は何の保証にもならない。
    """

    @field_validator("granularity")
    @classmethod
    def _check_granularity(cls, value: str) -> str:
        allowed = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"未知の足種です: {value}（利用可能: {', '.join(sorted(allowed))}）")
        return upper


class NotificationSettings(_Base):
    console: bool = True
    webhook_url: str | None = None
    """Discord / Slack の Incoming Webhook URL。"""

    webhook_kind: Literal["discord", "slack"] = "discord"
    min_level: Literal["debug", "info", "warning", "error"] = "info"


class StoreSettings(_Base):
    """SQLite 記録層の設定。

    ダッシュボードとレポートはこの DB を **別プロセスから** 読む。
    無効にすると両方とも表示するものが無くなる。
    """

    enabled: bool = True
    path: Path | None = None
    """既定は ``state_dir/zerotrade.db``。"""

    equity_interval_seconds: float = Field(default=60.0, gt=0, le=86_400)
    """equity のスナップショットを残す間隔。毎ループ書くと行が増えすぎる。"""

    record_signals: bool = True
    """HOLD 以外のシグナルを残すか。"""


class LoggingSettings(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # YAML 側のキーは ``json``。pydantic の BaseModel.json と衝突するため
    # フィールド名だけ別にしてエイリアスで受ける。
    json_output: bool = Field(default=False, alias="json")
    file: Path | None = None


class Settings(_Base):
    """アプリケーション全体の設定。"""

    mode: TradingMode = "paper"
    symbols: list[str] = Field(default_factory=lambda: ["USD_JPY"])
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=3600)
    state_dir: Path = Path("state")

    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    sizing: SizingSettings = Field(default_factory=SizingSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    store: StoreSettings = Field(default_factory=StoreSettings)

    @property
    def database_path(self) -> Path:
        """記録層の SQLite ファイルの場所。"""
        return self.store.path or (self.state_dir / "zerotrade.db")

    @model_validator(mode="after")
    def _check_symbols(self) -> Settings:
        if not self.symbols:
            raise ValueError("symbols を1つ以上指定してください")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols に重複があります")
        return self

    @model_validator(mode="after")
    def _check_live_requirements(self) -> Settings:
        # ライブ運用でペーパーブローカーを使うのは、ほぼ確実に設定ミス。
        if self.mode == "live" and self.broker.name == "paper":
            raise ValueError("mode=live で broker.name=paper は指定できません")
        return self


def _expand_env(value: Any) -> Any:
    """``${VAR}`` / ``${VAR:-default}`` を再帰的に環境変数へ置換する。

    値が丸ごと ``${VAR}`` で未定義の場合は ``None`` を返す
    （空文字にすると「トークンが設定済み」と誤認されるため）。
    """
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    whole = _ENV_PATTERN.fullmatch(value)
    if whole is not None:
        resolved = os.environ.get(whole.group(1))
        if resolved is None:
            return whole.group(2)  # デフォルト無しなら None
        return resolved

    def _sub(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1)) or match.group(2) or ""

    return _ENV_PATTERN.sub(_sub, value)


def load_settings(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """YAML を読み込んで :class:`Settings` を返す。

    Args:
        path: 設定ファイル。``None`` なら既定値のみで構築する。
        overrides: YAML より優先して適用するトップレベルキー
            （CLI の ``--mode`` など）。

    Raises:
        ConfigError: ファイルが無い / パースできない / 検証に失敗した場合。
    """
    raw: dict[str, Any] = {}

    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise ConfigError(f"設定ファイルが見つかりません: {config_path}")
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"設定ファイルのパースに失敗しました: {config_path}: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"設定ファイルのトップレベルはマッピングである必要があります: {config_path}"
            )
        raw = _expand_env(loaded)

    for key, value in (overrides or {}).items():
        if value is not None:
            raw[key] = value

    try:
        return Settings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError を含む
        raise ConfigError(f"設定の検証に失敗しました: {exc}") from exc
