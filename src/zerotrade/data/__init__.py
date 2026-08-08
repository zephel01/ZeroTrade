"""マーケットデータ層。"""

from __future__ import annotations

from zerotrade.data.align import align_candles
from zerotrade.data.feed import BrokerFeed, MarketDataFeed, StaticFeed
from zerotrade.data.fetcher import fetch_candles, save_csv
from zerotrade.data.historical import load_csv, synthetic_candles
from zerotrade.data.importer import GRANULARITIES, detect_format, read_any, resample
from zerotrade.data.providers import PROVIDERS, DataProvider, create_provider

__all__ = [
    "GRANULARITIES",
    "PROVIDERS",
    "BrokerFeed",
    "DataProvider",
    "MarketDataFeed",
    "StaticFeed",
    "align_candles",
    "create_provider",
    "detect_format",
    "fetch_candles",
    "load_csv",
    "read_any",
    "resample",
    "save_csv",
    "synthetic_candles",
]
