"""
Phase 1: One-time historical BTC/USDT klines ingestion → QuestDB
Fetches 1 year of 1-hour candles from Bybit and stores them locally.
"""

import requests
import time
from datetime import datetime, timezone

# --- CONFIG ---
SYMBOL = "BTCUSDT"
INTERVAL = "60"        # Bybit uses minutes: 60 = 1h
DAYS_BACK = 365
BYBIT_URL = "https://api.bybit.com/v5/market/kline"
DB_URL = "http://localhost:9000/exec"
BATCH_SIZE = 200       # Bybit max per request (200 candles = 200 hours per window)


def create_table():
    sql = """
    CREATE TABLE IF NOT EXISTS btc_klines (
        ts TIMESTAMP,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE,
        volume DOUBLE
    ) TIMESTAMP(ts) PARTITION BY MONTH;
    """
    r = requests.get(DB_URL, params={"query": sql})
    if r.status_code == 200:
        print("✅ Table btc_klines ready.")
    else:
        print(f"❌ Table creation failed: {r.text}")
        exit(1)


def fetch_klines(start_ms, end_ms):
    """Fetch up to 200 klines from Bybit between start_ms and end_ms."""
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "start": start_ms,
        "end": end_ms,
        "limit": BATCH_SIZE,
    }
    r = requests.get(BYBIT_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data["retCode"] != 0:
        raise ValueError(f"Bybit API error: {data['retMsg']}")
    # Bybit returns newest-first, so reverse to get chronological order
    return list(reversed(data["result"]["list"]))


def insert_batch(rows):
    """Bulk insert klines into QuestDB."""
    values = []
    for row in rows:
        # Bybit row: [startTime, open, high, low, close, volume, turnover]
        ts_us = int(row[0]) * 1000  # ms → microseconds for QuestDB
        open_, high, low, close = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        volume = float(row[5])
        values.append(f"({ts_us}, {open_}, {high}, {low}, {close}, {volume})")

    sql = f"INSERT INTO btc_klines VALUES {', '.join(values)};"
    r = requests.get(DB_URL, params={"query": sql})
    if r.status_code != 200:
        print(f"⚠️  Insert warning: {r.text[:200]}")


def main():
    create_table()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (DAYS_BACK * 24 * 60 * 60 * 1000)

    total_inserted = 0
    cursor = start_ms

    print(f"\n📥 Fetching {DAYS_BACK} days of 1h klines for {SYMBOL} via Bybit...")
    print(f"   From: {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"   To:   {datetime.fromtimestamp(now_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')}\n")

    # Bybit returns the LATEST rows in a range, so paginate by fixed forward windows
    window_ms = BATCH_SIZE * 60 * 60 * 1000  # 200 hours per window

    while cursor < now_ms:
        batch_end = min(cursor + window_ms, now_ms)
        try:
            rows = fetch_klines(cursor, batch_end)
            if rows:
                insert_batch(rows)
                total_inserted += len(rows)
                last_dt = datetime.fromtimestamp(int(rows[-1][0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            else:
                last_dt = "no data"

            cursor = batch_end + 1
            pct = min(100, (cursor - start_ms) / (now_ms - start_ms) * 100)
            print(f"   [{pct:5.1f}%] Total: {total_inserted} rows | Window end: {last_dt}")

            time.sleep(0.5)  # Stay well within Bybit rate limits

        except requests.RequestException as e:
            print(f"❌ Network error: {e}. Retrying in 5s...")
            time.sleep(5)
        except ValueError as e:
            if "Rate Limit" in str(e):
                print(f"⏳ Rate limited. Waiting 10s...")
                time.sleep(10)
            else:
                print(f"❌ API error: {e}")
                break

    print(f"\n✅ Done. Total rows inserted: {total_inserted}")
    print(f"   Verify: SELECT count() FROM btc_klines;")


if __name__ == "__main__":
    main()
