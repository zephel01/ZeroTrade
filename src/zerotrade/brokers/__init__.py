"""ブローカーアダプタ層。

新しいブローカーを足す手順:

1. :class:`~zerotrade.brokers.base.BaseBroker` を継承したクラスを書く
2. :func:`register_broker` で登録する
3. 設定の ``broker.name`` にその名前を書く

コア層のコードは一切変更しなくてよい。
"""

from __future__ import annotations

from collections.abc import Callable

from zerotrade.brokers.base import BaseBroker
from zerotrade.brokers.paper import PaperBroker
from zerotrade.errors import BrokerError, ConfigError
from zerotrade.log import get_logger
from zerotrade.settings import Settings

__all__ = [
    "BaseBroker",
    "PaperBroker",
    "available_brokers",
    "create_broker",
    "register_broker",
]

logger = get_logger(__name__)

BrokerFactory = Callable[[Settings], BaseBroker]

_REGISTRY: dict[str, BrokerFactory] = {}


def register_broker(name: str, factory: BrokerFactory) -> None:
    """ブローカーのファクトリを登録する。"""
    _REGISTRY[name] = factory


def available_brokers() -> list[str]:
    return sorted(_REGISTRY)


def _paper_factory(settings: Settings) -> BaseBroker:
    return PaperBroker(
        symbols=list(settings.symbols),
        initial_balance=settings.broker.initial_balance,
        currency=settings.broker.account_currency,
        spread=settings.broker.spread,
        leverage=settings.risk.assumed_leverage,
        contract_size=settings.sizing.contract_size,
    )


def _oanda_factory(settings: Settings) -> BaseBroker:
    from zerotrade.brokers.oanda import build_from_settings

    return build_from_settings(settings.broker)


def _ccxt_factory(settings: Settings) -> BaseBroker:
    from zerotrade.brokers.ccxt_broker import build_from_settings

    return build_from_settings(settings.broker, symbols=list(settings.symbols))


def _bingx_factory(settings: Settings) -> BaseBroker:
    from zerotrade.brokers.bingx import build_from_settings

    return build_from_settings(settings.broker, symbols=list(settings.symbols))


def _shadow_factory(settings: Settings) -> BaseBroker:
    from zerotrade.brokers.shadow import build_from_settings

    return build_from_settings(settings)


register_broker("paper", _paper_factory)
register_broker("oanda", _oanda_factory)
register_broker("ccxt", _ccxt_factory)
register_broker("bingx", _bingx_factory)
register_broker("shadow", _shadow_factory)


def create_broker(settings: Settings) -> BaseBroker:
    """設定からブローカーを構築する。

    ``broker.fallback_to_paper`` が有効なとき、認証情報の不足で
    ライブ用アダプタを作れなければ PaperBroker へ切り替える。
    APIキー未設定でも動作確認ができるようにするための挙動で、
    ``mode: live`` では設定バリデータが paper を禁止しているため発動しない。
    """
    factory = _REGISTRY.get(settings.broker.name)
    if factory is None:
        raise ConfigError(
            f"未知のブローカーです: {settings.broker.name}"
            f"（利用可能: {', '.join(available_brokers())}）"
        )

    try:
        return factory(settings)
    except (BrokerError, ConfigError):
        # ccxt は依存や取引所IDの不備を ConfigError で返すため、両方を拾う。
        if settings.broker.name == "paper" or not settings.broker.fallback_to_paper:
            raise
        logger.warning(
            "%s を初期化できなかったため PaperBroker で起動します"
            "（認証情報を設定すると実接続に切り替わります）",
            settings.broker.name,
        )
        return _paper_factory(settings)
