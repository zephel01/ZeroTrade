"""Bitstamp から BTC/USD の1時間足を長期取得する（検定用の独立データ）。

BingX(VST) とは取引所も商品性（現物 / 無期限先物）も違う。同じ現象が
両方で見えるなら単一取引所の癖ではない、と言えるようにするためのもの。

    python3 scripts/fetch_bitstamp.py

2017年以降を1000本ずつ取り、``data/btc_bitstamp_h1.csv`` に書く。
"""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=3600&limit=1000&start={start}"
START = int(datetime(2017, 1, 1, tzinfo=UTC).timestamp())
END = int(datetime(2026, 8, 8, tzinfo=UTC).timestamp())


def main() -> None:
    rows: dict[int, list[str]] = {}
    cursor = START
    while cursor < END:
        with urllib.request.urlopen(URL.format(start=cursor), timeout=30) as response:
            batch = json.load(response)["data"]["ohlc"]
        if not batch:
            break
        for candle in batch:
            rows[int(candle["timestamp"])] = [
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            ]
        newest = max(int(c["timestamp"]) for c in batch)
        if newest <= cursor:
            break
        cursor = newest + 3600
        print(f"\r{datetime.fromtimestamp(newest, UTC):%Y-%m-%d} 累計 {len(rows)}本", end="")
        time.sleep(0.35)

    out = Path("data/btc_bitstamp_h1.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts in sorted(rows):
            writer.writerow([datetime.fromtimestamp(ts, UTC).isoformat(), *rows[ts]])
    print(f"\n{out}: {len(rows)}本")


if __name__ == "__main__":
    main()
