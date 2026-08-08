"""設定から実行環境一式を組み立てる。

CLI・テスト・将来のバックテストが同じ組み立て手順を共有できるよう、
依存関係の配線をここ1か所に集約している。
"""

from __future__ import annotations

from dataclasses import dataclass

from zerotrade.brokers import create_broker
from zerotrade.brokers.base import BaseBroker
from zerotrade.core.notifier import Notifier, build_notifier
from zerotrade.core.orders import OrderManager
from zerotrade.core.risk import RiskManager
from zerotrade.core.runner import StrategyRunner
from zerotrade.core.sizing import PositionSizer
from zerotrade.data.feed import BrokerFeed
from zerotrade.errors import ConfigError
from zerotrade.log import get_logger
from zerotrade.settings import Settings
from zerotrade.store import Store
from zerotrade.strategies import create_strategy

__all__ = ["Application", "build_application"]

logger = get_logger(__name__)


@dataclass(slots=True)
class Application:
    """組み立て済みのコンポーネント一式。"""

    settings: Settings
    broker: BaseBroker
    risk: RiskManager
    runner: StrategyRunner
    notifier: Notifier
    store: Store | None = None

    async def aclose(self) -> None:
        """確保したリソースを解放する。"""
        await self.notifier.aclose()
        if self.store is not None:
            self.store.close()


def _check_live_guard(settings: Settings, broker: BaseBroker) -> None:
    """``mode`` に実弾を止める力を持たせる。

    ``mode`` はこれまで表示用でしかなく、``mode: paper`` のまま
    ``environment: live`` の実ブローカーを指定すると**本物の注文が飛んだ**。
    設定に paper と書いてあるのに実弾が動くのは、最悪の裏切り方である。

    実在の取引所（:attr:`~zerotrade.brokers.base.BaseBroker.is_simulated`
    が False）へ本番環境で接続する構成は、``mode: live`` と明示した
    ときだけ許す。テストネットと擬似ブローカーは対象外。
    """
    if broker.is_simulated or settings.broker.environment != "live":
        return
    if settings.mode == "live":
        return
    raise ConfigError(
        f"mode={settings.mode} のまま broker={broker.name} を environment=live で"
        f"使うことはできません。この構成は実在の取引所へ本物の注文を送ります。"
        f"実弾を入れる意図があるなら mode: live と明記してください"
    )


def build_application(settings: Settings) -> Application:
    """設定から Broker / RiskManager / StrategyRunner を構築する。"""
    broker = create_broker(settings)
    _check_live_guard(settings, broker)

    risk = RiskManager.load(
        settings.risk,
        settings.state_dir / "risk_state.json",
        contract_size=settings.sizing.contract_size,
    )
    if risk.is_halted:
        logger.warning(
            "前回の状態を引き継ぎ、取引は停止中です（理由: %s）。"
            "解除するには `zerotrade resume` を実行してください。",
            risk.state.halt_reason,
        )

    sizer = PositionSizer(settings.sizing, settings.risk)
    strategy = create_strategy(settings.strategy.name, settings.strategy.params)
    notifier = build_notifier(settings.notifications)
    store = Store(settings.database_path) if settings.store.enabled else None

    runner = StrategyRunner(
        settings=settings,
        broker=broker,
        feed=BrokerFeed(broker),
        strategy=strategy,
        risk=risk,
        sizer=sizer,
        orders=OrderManager(broker, risk),
        notifier=notifier,
        store=store,
    )

    logger.info(
        "構成: mode=%s broker=%s strategy=%s symbols=%s",
        settings.mode,
        broker.name,
        strategy.name,
        ", ".join(settings.symbols),
    )
    return Application(
        settings=settings,
        broker=broker,
        risk=risk,
        runner=runner,
        notifier=notifier,
        store=store,
    )
