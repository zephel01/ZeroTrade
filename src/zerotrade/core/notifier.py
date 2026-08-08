"""通知（Discord / Slack Webhook・コンソール）。

通知の失敗が取引ロジックを止めてはいけない。
そのため送信エラーはすべてログに落として握りつぶす。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Literal, Self

import httpx

from zerotrade.log import get_logger
from zerotrade.settings import NotificationSettings

__all__ = [
    "CompositeNotifier",
    "ConsoleNotifier",
    "Level",
    "Notifier",
    "NullNotifier",
    "WebhookNotifier",
    "build_notifier",
]

Level = Literal["debug", "info", "warning", "error"]

_LEVEL_ORDER: dict[Level, int] = {"debug": 10, "info": 20, "warning": 30, "error": 40}

logger = get_logger(__name__)


class Notifier(ABC):
    """通知先の抽象。"""

    @abstractmethod
    async def send(self, message: str, *, level: Level = "info") -> None:
        """メッセージを送る。実装は例外を送出してはならない。"""

    async def aclose(self) -> None:
        """リソースを解放する。既定では何もしない（抽象ではない）。"""
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class NullNotifier(Notifier):
    """何もしない通知先。テストや backtest で使う。"""

    async def send(self, message: str, *, level: Level = "info") -> None:
        return None


class ConsoleNotifier(Notifier):
    """ログへ書き出すだけの通知先。"""

    def __init__(self, min_level: Level = "info") -> None:
        self._min = _LEVEL_ORDER[min_level]

    async def send(self, message: str, *, level: Level = "info") -> None:
        if _LEVEL_ORDER[level] < self._min:
            return
        logger.log(_LEVEL_ORDER[level], "[notify] %s", message)


class WebhookNotifier(Notifier):
    """Discord / Slack の Incoming Webhook へ POST する。"""

    def __init__(
        self,
        url: str,
        *,
        kind: Literal["discord", "slack"] = "discord",
        min_level: Level = "info",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._kind = kind
        self._min = _LEVEL_ORDER[min_level]
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def send(self, message: str, *, level: Level = "info") -> None:
        if _LEVEL_ORDER[level] < self._min:
            return
        # Discord は "content"、Slack は "text" をボディのキーに使う。
        key = "content" if self._kind == "discord" else "text"
        try:
            response = await self._client.post(self._url, json={key: message})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # 通知の失敗で取引を止めない。
            logger.warning("Webhook通知に失敗しました: %s", exc)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class CompositeNotifier(Notifier):
    """複数の通知先へ同じメッセージを配る。"""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers

    async def send(self, message: str, *, level: Level = "info") -> None:
        for notifier in self._notifiers:
            await notifier.send(message, level=level)

    async def aclose(self) -> None:
        for notifier in self._notifiers:
            await notifier.aclose()


def build_notifier(settings: NotificationSettings) -> Notifier:
    """設定から通知先を組み立てる。"""
    notifiers: list[Notifier] = []
    if settings.console:
        notifiers.append(ConsoleNotifier(min_level=settings.min_level))
    if settings.webhook_url:
        notifiers.append(
            WebhookNotifier(
                settings.webhook_url,
                kind=settings.webhook_kind,
                min_level=settings.min_level,
            )
        )
    if not notifiers:
        return NullNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return CompositeNotifier(notifiers)
