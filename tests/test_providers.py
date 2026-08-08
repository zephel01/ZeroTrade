"""公開データソースのテスト（respx で HTTP をモック）。

実エンドポイントには到達せずに、応答形式の解釈だけを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from zerotrade.data.providers import (
    PROVIDERS,
    StooqProvider,
    YahooProvider,
    create_provider,
)
from zerotrade.errors import BrokerError, ConfigError

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/JPY=X"
STOOQ_URL = "https://stooq.com/q/d/l/"


def _yahoo_payload() -> dict[str, object]:
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": [1704153600, 1704157200, 1704160800],
                    "indicators": {
                        "quote": [
                            {
                                "open": [140.1, 140.5, None],
                                "high": [140.9, 140.8, None],
                                "low": [139.8, 140.2, None],
                                "close": [140.5, 140.3, None],
                                "volume": [100, 120, None],
                            }
                        ]
                    },
                }
            ],
        }
    }


# ------------------------------------------------------------ レジストリ


def test_プロバイダを名前から作れる() -> None:
    assert isinstance(create_provider("yahoo"), YahooProvider)
    assert isinstance(create_provider("stooq"), StooqProvider)
    assert set(PROVIDERS) == {"yahoo", "stooq"}


def test_未知のプロバイダは拒否される() -> None:
    with pytest.raises(ConfigError, match="未知のデータソース"):
        create_provider("bloomberg")


# ------------------------------------------------------------ Yahoo


@respx.mock
async def test_Yahooの応答を足に変換できる() -> None:
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=_yahoo_payload()))

    provider = YahooProvider()
    candles = await provider.fetch("USD_JPY", granularity="H1", days=30)
    await provider.aclose()

    # null を含む3本目は捨てる。
    assert len(candles) == 2
    assert candles[0].timestamp == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    assert candles[0].open == Decimal("140.1")
    assert candles[0].high == Decimal("140.9")
    assert candles[-1].close == Decimal("140.3")


@respx.mock
async def test_Yahooの銘柄表記へ変換される() -> None:
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=_yahoo_payload()))
    provider = YahooProvider()
    await provider.fetch("USD_JPY", granularity="D1", days=10)
    await provider.aclose()

    assert route.called, "USD_JPY が JPY=X に変換されていない"
    assert route.calls.last.request.url.params["interval"] == "1d"


@respx.mock
async def test_Yahooは上限を超える日数を切り詰める() -> None:
    """黙って短い期間を返すと「取れたつもり」の検証をしてしまう。"""
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=_yahoo_payload()))
    provider = YahooProvider()
    await provider.fetch("USD_JPY", granularity="M5", days=9999)
    await provider.aclose()

    params = route.calls.last.request.url.params
    span = int(params["period2"]) - int(params["period1"])
    assert span <= 61 * 86_400, "5分足の60日上限を超えて要求している"


async def test_Yahooが対応しない足種は拒否される() -> None:
    provider = YahooProvider()
    with pytest.raises(ConfigError, match="足種"):
        await provider.fetch("USD_JPY", granularity="H4")
    await provider.aclose()


@respx.mock
async def test_Yahooのエラー応答はBrokerError() -> None:
    respx.get(YAHOO_URL).mock(
        return_value=httpx.Response(
            200, json={"chart": {"error": {"code": "Not Found"}, "result": None}}
        )
    )
    provider = YahooProvider()
    with pytest.raises(BrokerError, match="エラー"):
        await provider.fetch("USD_JPY", granularity="D1")
    await provider.aclose()


@respx.mock
async def test_HTTPエラーはBrokerErrorへ正規化される() -> None:
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(429, text="Too Many Requests"))
    provider = YahooProvider()
    with pytest.raises(BrokerError, match="429"):
        await provider.fetch("USD_JPY", granularity="D1")
    await provider.aclose()


@respx.mock
async def test_通信失敗もBrokerErrorへ正規化される() -> None:
    respx.get(YAHOO_URL).mock(side_effect=httpx.ConnectError("network down"))
    provider = YahooProvider()
    with pytest.raises(BrokerError, match="通信"):
        await provider.fetch("USD_JPY", granularity="D1")
    await provider.aclose()


# ------------------------------------------------------------ Stooq


@respx.mock
async def test_StooqのCSVを足に変換できる() -> None:
    csv_text = (
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-02,140.100,140.900,139.800,140.500,0\n"
        "2024-01-03,140.500,141.200,140.300,141.000,0\n"
    )
    respx.get(STOOQ_URL).mock(return_value=httpx.Response(200, text=csv_text))

    provider = StooqProvider()
    candles = await provider.fetch("USD_JPY", granularity="D1", days=100_000)
    await provider.aclose()

    assert len(candles) == 2
    assert candles[0].timestamp == datetime(2024, 1, 2, tzinfo=UTC)
    assert candles[-1].close == Decimal("141.000")


async def test_Stooqは日足以外を拒否する() -> None:
    provider = StooqProvider()
    with pytest.raises(ConfigError, match="日足"):
        await provider.fetch("USD_JPY", granularity="H1")
    await provider.aclose()


@respx.mock
async def test_Stooqがデータ無しを返したらBrokerError() -> None:
    respx.get(STOOQ_URL).mock(return_value=httpx.Response(200, text="No data"))
    provider = StooqProvider()
    with pytest.raises(BrokerError, match="データがありません"):
        await provider.fetch("USD_JPY", granularity="D1")
    await provider.aclose()


@respx.mock
async def test_Stooqは期間外を切り落とす() -> None:
    csv_text = (
        "Date,Open,High,Low,Close,Volume\n"
        "1990-01-02,140.1,140.9,139.8,140.5,0\n"
        "2099-01-02,140.5,141.2,140.3,141.0,0\n"
    )
    respx.get(STOOQ_URL).mock(return_value=httpx.Response(200, text=csv_text))

    provider = StooqProvider()
    candles = await provider.fetch("USD_JPY", granularity="D1", days=30)
    await provider.aclose()

    assert len(candles) == 1
    assert candles[0].timestamp.year == 2099
