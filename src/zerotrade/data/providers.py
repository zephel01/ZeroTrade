"""認証不要の公開ソースからヒストリカルデータを取得する。

ブローカーのAPIには口座条件がつきものなので（OANDA証券なら本番口座・
プロコース・残高25万円以上）、検証を始めるだけのために口座を作るのは順序が逆である。
ここでは無料で誰でも叩けるソースを使い、口座が整うまでの間も
戦略の検証を進められるようにする。

対応ソース:

``yahoo``
    Yahoo Finance のチャートAPI。日足なら数十年、1時間足なら直近2年、
    5分足なら直近60日ぶんが取れる。銘柄は ``JPY=X`` のような表記になる。
``stooq``
    Stooq のCSVエンドポイント。日足のみだが非常に安定していて軽い。

いずれも**非公式・無保証のエンドポイント**である。実運用の発注に使うものではなく、
戦略のふるい分けに使う位置づけと考えること。歯抜けや値の癖もあるので、
本命の検証はブローカーから取った足で行うのが望ましい。
"""

from __future__ import annotations

import csv
import io
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from zerotrade.errors import BrokerError, ConfigError
from zerotrade.log import get_logger
from zerotrade.models import Candle, to_decimal, utcnow

__all__ = ["PROVIDERS", "DataProvider", "StooqProvider", "YahooProvider", "create_provider"]

logger = get_logger(__name__)

#: ZeroTrade の銘柄表記 → 各ソースの表記。
_YAHOO_SYMBOLS = {
    "USD_JPY": "JPY=X",
    "EUR_JPY": "EURJPY=X",
    "GBP_JPY": "GBPJPY=X",
    "AUD_JPY": "AUDJPY=X",
    "EUR_USD": "EURUSD=X",
    "GBP_USD": "GBPUSD=X",
}

_STOOQ_SYMBOLS = {
    "USD_JPY": "usdjpy",
    "EUR_JPY": "eurjpy",
    "GBP_JPY": "gbpjpy",
    "AUD_JPY": "audjpy",
    "EUR_USD": "eurusd",
    "GBP_USD": "gbpusd",
}

#: ZeroTrade の足種 → Yahoo の interval。
_YAHOO_INTERVALS = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "D1": "1d",
}

#: Yahoo が interval ごとに遡れる日数の上限。超えると空応答になる。
_YAHOO_MAX_DAYS = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "1h": 730,
    "1d": 20_000,
}


class DataProvider(ABC):
    """公開ソースの共通インターフェース。"""

    name: str = "base"

    def __init__(self, *, timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        self._external = client
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            # UA を名乗らないと弾く配信元があるため明示する。
            headers={"User-Agent": "ZeroTrade/0.1 (historical data fetch)"},
        )

    @abstractmethod
    async def fetch(self, symbol: str, *, granularity: str = "D1", days: int = 365) -> list[Candle]:
        """足を古い順に返す。"""

    async def aclose(self) -> None:
        if self._external is None:
            await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET し、失敗はすべて BrokerError へ正規化する。"""
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BrokerError(f"{self.name} への通信に失敗しました: {exc}") from exc

        if response.status_code >= 400:
            raise BrokerError(
                f"{self.name} が {response.status_code} を返しました: {response.text[:200]}"
            )
        return response


class YahooProvider(DataProvider):
    """Yahoo Finance のチャートAPI。"""

    name = "yahoo"
    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

    async def fetch(self, symbol: str, *, granularity: str = "D1", days: int = 365) -> list[Candle]:
        key = granularity.strip().upper()
        interval = _YAHOO_INTERVALS.get(key)
        if interval is None:
            raise ConfigError(
                f"yahoo は足種 {granularity} に対応していません"
                f"（利用可能: {', '.join(_YAHOO_INTERVALS)}）"
            )

        limit = _YAHOO_MAX_DAYS[interval]
        if days > limit:
            # 黙って短い期間を返すと「取れたつもり」の検証をしてしまう。
            logger.warning(
                "yahoo の %s は最大 %d 日ぶんまでです。%d 日 → %d 日に切り詰めます。",
                interval,
                limit,
                days,
                limit,
            )
            days = limit

        ticker = _YAHOO_SYMBOLS.get(symbol, symbol)
        end = utcnow()
        start = end - timedelta(days=days)

        response = await self._get(
            f"{self.BASE_URL}/{ticker}",
            params={
                "interval": interval,
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
            },
        )
        return self._parse(response.json(), symbol)

    @staticmethod
    def _parse(payload: Any, symbol: str) -> list[Candle]:
        chart = (payload or {}).get("chart") or {}
        if chart.get("error"):
            raise BrokerError(f"yahoo がエラーを返しました: {chart['error']}")

        results = chart.get("result") or []
        if not results:
            raise BrokerError("yahoo の応答に価格データがありません")

        result = results[0]
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        candles: list[Candle] = []
        for i, stamp in enumerate(stamps):
            values = [
                _at(opens, i),
                _at(highs, i),
                _at(lows, i),
                _at(closes, i),
            ]
            # 休場の区間は null が入る。歯抜けの足を作らず捨てる。
            if any(v is None for v in values):
                continue
            open_, high, low, close = values
            candles.append(
                Candle(
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(int(stamp), tz=UTC),
                    open=to_decimal(str(open_)),
                    high=to_decimal(str(high)),
                    low=to_decimal(str(low)),
                    close=to_decimal(str(close)),
                    volume=to_decimal(str(_at(volumes, i) or 0)),
                )
            )

        candles.sort(key=lambda c: c.timestamp)
        return candles


class StooqProvider(DataProvider):
    """Stooq の CSV エンドポイント。日足のみ。"""

    name = "stooq"
    BASE_URL = "https://stooq.com/q/d/l/"

    async def fetch(self, symbol: str, *, granularity: str = "D1", days: int = 365) -> list[Candle]:
        if granularity.strip().upper() != "D1":
            raise ConfigError("stooq は日足（D1）のみ対応しています")

        ticker = _STOOQ_SYMBOLS.get(symbol, symbol.replace("_", "").lower())
        response = await self._get(self.BASE_URL, params={"s": ticker, "i": "d"})

        text = response.text.strip()
        if not text or text.lower().startswith("no data"):
            raise BrokerError(f"stooq に {ticker} のデータがありません")

        cutoff = utcnow() - timedelta(days=days)
        candles: list[Candle] = []
        for row in csv.DictReader(io.StringIO(text)):
            raw = (row.get("Date") or "").strip()
            if not raw:
                continue
            try:
                stamp = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
                if stamp < cutoff:
                    continue
                candles.append(
                    Candle(
                        symbol=symbol,
                        timestamp=stamp,
                        open=to_decimal(row["Open"]),
                        high=to_decimal(row["High"]),
                        low=to_decimal(row["Low"]),
                        close=to_decimal(row["Close"]),
                        volume=to_decimal(row.get("Volume") or 0),
                    )
                )
            except (ValueError, KeyError, ArithmeticError) as exc:
                raise BrokerError(f"stooq の応答を解釈できませんでした: {exc}") from exc

        if not candles:
            raise BrokerError(f"stooq から {ticker} の足を取得できませんでした")

        candles.sort(key=lambda c: c.timestamp)
        return candles


def _at(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


PROVIDERS: dict[str, type[DataProvider]] = {
    "yahoo": YahooProvider,
    "stooq": StooqProvider,
}


def create_provider(name: str, **kwargs: Any) -> DataProvider:
    """名前からプロバイダを生成する。

    Raises:
        ConfigError: 未知の名前の場合。
    """
    cls = PROVIDERS.get(name.strip().lower())
    if cls is None:
        raise ConfigError(
            f"未知のデータソースです: {name}（利用可能: {', '.join(sorted(PROVIDERS))}）"
        )
    return cls(**kwargs)
