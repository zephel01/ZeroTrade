"""BingX の公開APIから1時間足を長期取得する（認証不要・発注なし）。

``zerotrade fetch`` は口座に紐づくアダプタ経由なので、APIキーとVST環境が要る。
こちらは公開の kline エンドポイントだけを使うため、鍵が無くても
本番の板・歩み値に基づくデータを取れる。**検証用のデータ取得はこちらが適切**で、
VST（デモ）はスプレッドが本番と大きく違うため取引条件の参考にしない。

    python3 scripts/fetch_bingx_public.py SOL-USDT TAO-USDT
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

URL = (
    "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
    "?symbol={symbol}&interval=1h&limit=1000&startTime={start}&endTime={end}"
)
HOUR_MS = 3_600_000


def fetch(symbol: str, start_ms: int, end_ms: int) -> dict[int, list[str]]:
    rows: dict[int, list[str]] = {}
    cursor = start_ms
    while cursor < end_ms:
        url = URL.format(symbol=symbol, start=cursor, end=min(cursor + 1000 * HOUR_MS, end_ms))
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.load(response)
        batch = payload.get("data") or []
        if not batch:
            cursor += 1000 * HOUR_MS
            continue
        for candle in batch:
            ts = int(candle["time"])
            rows[ts] = [
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            ]
        newest = max(int(c["time"]) for c in batch)
        cursor = max(newest + HOUR_MS, cursor + HOUR_MS)
        stamp = datetime.fromtimestamp(newest / 1000, UTC)
        print(f"\r{symbol}: {stamp:%Y-%m-%d} {len(rows)}本", end="")
        time.sleep(0.3)
    return rows


def main() -> None:
    symbols = sys.argv[1:] or ["SOL-USDT"]
    end_ms = int(datetime(2026, 8, 8, 12, tzinfo=UTC).timestamp() * 1000)
    start_ms = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)
    for symbol in symbols:
        rows = fetch(symbol, start_ms, end_ms)
        if not rows:
            print(f"\n{symbol}: 取得できませんでした")
            continue
        out = Path("data") / f"{symbol.lower().replace('-', '_')}_h1.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for ts in sorted(rows):
                writer.writerow([datetime.fromtimestamp(ts / 1000, UTC).isoformat(), *rows[ts]])
        print(f"\n{out}: {len(rows)}本")


if __name__ == "__main__":
    main()
