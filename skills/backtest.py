"""
Backtesting engine — runs a strategy config against historical btc_klines.
Returns win rate, signal count, and Sharpe approximation.

Usage:
    from skills.backtest import run_backtest
    result = run_backtest(strategy_dict, lookahead_candles=4)
"""

import requests
import math

DB_URL = "http://localhost:9000/exec"


# ─── INDICATOR FUNCTIONS ──────────────────────────────────────────────────────

def _rsi(closes, period):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    avg_gain = sum(d for d in recent if d > 0) / period
    avg_loss = sum(-d for d in recent if d < 0) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return max(0.0, min(100.0, 100 - rsi))


def _ema(values, period):
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _score(closes, volumes, price, ref_price, p):
    """Compute composite score given params dict p."""
    # Momentum
    change_pct = (price - ref_price) / ref_price * 100 if ref_price else 0
    s_momentum = max(0.0, min(100.0, 50 + change_pct * p["momentum_scale"]))

    # RSI (oversold = high score)
    s_rsi = _rsi(closes, p["rsi_period"])

    # EMA trend
    if len(closes) >= p["ema_slow"]:
        ema_f = _ema(closes, p["ema_fast"])
        ema_s = _ema(closes, p["ema_slow"])
        pct = (ema_f - ema_s) / ema_s * 100 if ema_s else 0
        s_ema = max(0.0, min(100.0, 50 + pct * 40))
    else:
        s_ema = 50.0

    # Volume
    avg_vol = sum(volumes) / len(volumes) if volumes else 1
    ratio   = volumes[-1] / avg_vol if avg_vol else 1
    if ratio <= 0.8:
        s_vol = 50.0
    else:
        direction = 1 if price >= ref_price else -1
        spike = min(ratio - 1.0, p["volume_spike_cap"]) / p["volume_spike_cap"]
        s_vol = max(0.0, min(100.0, 50 + direction * spike * 40))

    w = p["weights"]
    score = (
        s_momentum * w["momentum"] +
        s_rsi      * w["rsi"] +
        s_ema      * w["ema_trend"] +
        s_vol      * w["volume"] +
        50.0       * w["accuracy"]   # Neutral for accuracy during backtest
    )
    return round(score), {
        "momentum": s_momentum, "rsi": s_rsi,
        "ema": s_ema, "volume": s_vol
    }


# ─── MAIN BACKTEST ────────────────────────────────────────────────────────────

def run_backtest(strategy, lookahead_candles=4):
    """
    Backtest a strategy dict against all of btc_klines.

    Returns:
        dict with win_rate, signals, sharpe, pnl_list, sample_signals
    """
    params = {**strategy["params"], "weights": strategy["weights"]}
    buy_thresh  = params["alert_buy_threshold"]
    sell_thresh = params["alert_sell_threshold"]
    warmup      = max(params["rsi_period"], params["ema_slow"]) + 2

    # Load full history
    r = requests.get(
        DB_URL,
        params={"query": "SELECT ts, open, high, low, close, volume FROM btc_klines ORDER BY ts ASC"},
        timeout=10,
    )
    rows = r.json().get("dataset", [])
    if len(rows) < warmup + lookahead_candles + 5:
        return {"error": "Not enough historical data"}

    closes  = [float(row[4]) for row in rows]
    volumes = [float(row[5]) for row in rows]

    wins, losses, pnl_list, signals = 0, 0, [], []

    for i in range(warmup, len(rows) - lookahead_candles):
        c_window = closes[max(0, i - 50): i + 1]
        v_window = volumes[max(0, i - 20): i + 1]
        price    = closes[i]
        ref      = closes[i - 1]

        score, _ = _score(c_window, v_window, price, ref, params)

        if score >= buy_thresh:
            future_price = closes[i + lookahead_candles]
            pnl = (future_price - price) / price * 100
            won = pnl > 0
        elif score <= sell_thresh:
            future_price = closes[i + lookahead_candles]
            pnl = (price - future_price) / price * 100
            won = pnl > 0
        else:
            continue

        direction = "BUY" if score >= buy_thresh else "SELL"
        pnl_list.append(pnl)
        signals.append({"ts": rows[i][0], "direction": direction, "score": score, "pnl": round(pnl, 3)})

        if won:
            wins += 1
        else:
            losses += 1

    total = wins + losses
    if total == 0:
        return {"error": "No signals generated — thresholds may be too tight"}

    win_rate = wins / total

    # Sharpe approximation: avg_pnl / std_pnl
    avg_pnl = sum(pnl_list) / len(pnl_list)
    variance = sum((p - avg_pnl) ** 2 for p in pnl_list) / len(pnl_list)
    std_pnl  = math.sqrt(variance) if variance > 0 else 0.0001
    sharpe   = avg_pnl / std_pnl

    return {
        "win_rate":       round(win_rate, 4),
        "wins":           wins,
        "losses":         losses,
        "total_signals":  total,
        "avg_pnl_pct":    round(avg_pnl, 4),
        "sharpe":         round(sharpe, 4),
        "sample_signals": signals[-5:],   # Last 5 for context
    }


if __name__ == "__main__":
    import json, os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    with open("strategy.json") as f:
        strategy = json.load(f)

    print("Running backtest on current strategy...")
    result = run_backtest(strategy, lookahead_candles=4)
    print(json.dumps(result, indent=2))
