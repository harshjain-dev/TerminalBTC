"""
BTC Terminal v2 — Weighted Signal Engine
Streams live BTC prices via Bybit, computes a multi-indicator confidence
score (0-100), and sends weighted signals to Telegram.
"""

import websocket
import json
import requests
import time
import os

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SYMBOL         = "BTCUSDT"
BYBIT_WS_URL   = "wss://stream.bybit.com/v5/public/linear"
DB_URL         = "http://localhost:9000/exec"
INR_API_URL    = "https://api.exchangerate-api.com/v4/latest/USD"
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
STRATEGY_FILE  = os.path.join(os.path.dirname(__file__), "strategy.json")

CANDLE_BUFFER  = 50    # Historical candles kept in memory
ALERT_COOLDOWN = 300   # Seconds between Telegram alerts (5 min)
SCORE_DELTA    = 15    # Min score change needed to re-alert
INR_REFRESH    = 3600  # Seconds between INR rate refreshes


def load_strategy():
    """Load strategy.json — hot-reloads when research agent deploys a new one."""
    with open(STRATEGY_FILE) as f:
        s = json.load(f)
    return s["weights"], s["params"]


WEIGHTS, STRATEGY_PARAMS = load_strategy()
SCORE_INTERVAL = STRATEGY_PARAMS["score_interval_secs"]

SCORE_LABELS = [
    (80, "🚀 STRONG BUY"),
    (60, "📈 LEAN BUY"),
    (40, "⚖️  NEUTRAL"),
    (20, "📉 LEAN SELL"),
    (0,  "🔻 STRONG SELL"),
]


# ─── INDICATORS ───────────────────────────────────────────────────────────────

def compute_rsi(closes, period=14):
    """Returns score 0-100. Low RSI (oversold) → high score (buy signal)."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    avg_gain = sum(d for d in recent if d > 0) / period
    avg_loss = sum(-d for d in recent if d < 0) / period
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    return max(0.0, min(100.0, 100 - rsi))


def compute_ema(values, period):
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def ema_trend_score(closes, ema_fast=9, ema_slow=21):
    """EMA crossover. Score >50 = bullish, <50 = bearish."""
    if len(closes) < ema_slow:
        return 50.0
    ema_f = compute_ema(closes, ema_fast)
    ema_s = compute_ema(closes, ema_slow)
    if ema_s == 0:
        return 50.0
    pct_diff = (ema_f - ema_s) / ema_s * 100
    return max(0.0, min(100.0, 50 + pct_diff * 40))


def momentum_score(current, reference):
    """Price change vs last hourly close. ±3% move → ±45 score points."""
    if reference == 0:
        return 50.0
    change_pct = (current - reference) / reference * 100
    return max(0.0, min(100.0, 50 + change_pct * 15))


def volume_score(current_vol, avg_vol, price_now, price_prev):
    """Volume spike directional signal. High vol + up move = buy signal."""
    if avg_vol == 0 or current_vol == 0:
        return 50.0
    ratio = current_vol / avg_vol
    if ratio <= 0.8:
        return 50.0
    direction = 1 if price_now >= price_prev else -1
    spike = min(ratio - 1.0, 1.5) / 1.5  # Normalise spike 0→1
    return max(0.0, min(100.0, 50 + direction * spike * 40))


def accuracy_score():
    """Score based on recent signal accuracy from QuestDB history."""
    query = (
        "SELECT direction, price, lead(price) OVER (ORDER BY timestamp) "
        "FROM btc_alerts ORDER BY timestamp DESC LIMIT 20"
    )
    try:
        r = requests.get(DB_URL, params={"query": query}, timeout=3)
        rows = r.json().get("dataset", [])
        valid = [(d, p, n) for d, p, n in rows if n is not None]
        if len(valid) < 3:
            return 50.0
        correct = sum(
            1 for d, p, n in valid
            if (d == "UP" and n > p) or (d == "DOWN" and n < p)
        )
        acc = correct / len(valid)
        return 20 + acc * 60  # Maps 0–100% accuracy to score 20–80
    except Exception:
        return 50.0


def get_label(score):
    for threshold, label in SCORE_LABELS:
        if score >= threshold:
            return label
    return SCORE_LABELS[-1][1]


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

class TelegramNotifier:
    def __init__(self):
        self.url        = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        self.last_sent  = 0
        self.last_score = 50

    def send(self, score, components, price_usd, inr_rate):
        in_cooldown     = (time.time() - self.last_sent) < ALERT_COOLDOWN
        score_unchanged = abs(score - self.last_score) < SCORE_DELTA
        is_neutral      = 40 <= score <= 60

        if in_cooldown or score_unchanged or is_neutral:
            return

        label = get_label(score)
        breakdown = "\n".join(
            f"  {k.capitalize():<12} {v:5.1f}/100  ×{WEIGHTS[k]:.0%}"
            for k, v in components.items()
        )
        msg = (
            f"{label}\n"
            f"{'─' * 30}\n"
            f"Score  : {score}/100\n"
            f"Price  : ${price_usd:,.2f}  |  ₹{price_usd * inr_rate:,.2f}\n\n"
            f"Signals:\n{breakdown}"
        )
        try:
            r = requests.post(
                self.url,
                data={"chat_id": CHAT_ID, "text": msg},
                timeout=5,
            )
            if r.status_code == 200:
                self.last_sent  = time.time()
                self.last_score = score
                print(f"   📲 Sent: {label} ({score}/100)")
        except Exception as e:
            print(f"   ❌ Telegram: {e}")


# ─── SIGNAL ENGINE ────────────────────────────────────────────────────────────

class SignalEngine:
    def __init__(self):
        self.closes       = []
        self.volumes      = []
        self.price        = None
        self.inr_rate     = 92.35
        self.last_scored  = time.time()
        self.last_inr     = 0
        self.current_vol  = 0.0
        self.notifier     = TelegramNotifier()
        self._setup_db()
        self._load_history()
        self._fetch_inr()

    def _setup_db(self):
        sql = """CREATE TABLE IF NOT EXISTS btc_signals (
            ts         TIMESTAMP,
            direction  SYMBOL,
            price      DOUBLE,
            inr_rate   DOUBLE,
            score      INT,
            s_momentum DOUBLE,
            s_rsi      DOUBLE,
            s_ema      DOUBLE,
            s_volume   DOUBLE,
            s_accuracy DOUBLE
        ) TIMESTAMP(ts) PARTITION BY MONTH;"""
        try:
            requests.get(DB_URL, params={"query": sql}, timeout=5)
            print("✅ Table btc_signals ready.")
        except Exception as e:
            print(f"⚠️  DB setup: {e}")

    def _load_history(self):
        q = (f"SELECT close, volume FROM btc_klines "
             f"ORDER BY ts DESC LIMIT {CANDLE_BUFFER}")
        try:
            r   = requests.get(DB_URL, params={"query": q}, timeout=5)
            rows = list(reversed(r.json().get("dataset", [])))
            self.closes  = [float(row[0]) for row in rows]
            self.volumes = [float(row[1]) for row in rows]
            print(f"📚 Loaded {len(rows)} historical candles.")
        except Exception as e:
            print(f"⚠️  History load failed: {e}")

    def _fetch_inr(self):
        try:
            r = requests.get(INR_API_URL, timeout=5)
            self.inr_rate = r.json()["rates"]["INR"]
            self.last_inr = time.time()
            print(f"🌍 1 USD = ₹{self.inr_rate:.2f}")
        except Exception as e:
            print(f"⚠️  INR fetch failed: {e}")

    def _log_signal(self, direction, price, score, c):
        sql = (
            f"INSERT INTO btc_signals VALUES("
            f"now(), '{direction}', {price}, {self.inr_rate}, {score}, "
            f"{c['momentum']:.2f}, {c['rsi']:.2f}, {c['ema_trend']:.2f}, "
            f"{c['volume']:.2f}, {c['accuracy']:.2f})"
        )
        try:
            requests.get(DB_URL, params={"query": sql}, timeout=2)
        except Exception:
            pass

    def on_trade(self, price, vol):
        self.price        = price
        self.current_vol += vol

        if time.time() - self.last_inr > INR_REFRESH:
            self._fetch_inr()

        # Hot-reload strategy if research agent deployed a new one
        global WEIGHTS, STRATEGY_PARAMS, SCORE_INTERVAL
        try:
            WEIGHTS, STRATEGY_PARAMS = load_strategy()
            SCORE_INTERVAL = STRATEGY_PARAMS["score_interval_secs"]
        except Exception:
            pass

        if time.time() - self.last_scored < SCORE_INTERVAL:
            return

        self.last_scored = time.time()

        if len(self.closes) < 15:
            print(f"  BTC ${price:>10,.2f}  |  Warming up indicators...")
            return

        ref_price = self.closes[-1]
        avg_vol   = sum(self.volumes) / len(self.volumes) if self.volumes else 1.0
        p         = STRATEGY_PARAMS

        components = {
            "momentum":  momentum_score(price, ref_price),
            "rsi":       compute_rsi(self.closes, p["rsi_period"]),
            "ema_trend": ema_trend_score(self.closes, p["ema_fast"], p["ema_slow"]),
            "volume":    volume_score(self.current_vol, avg_vol, price, ref_price),
            "accuracy":  accuracy_score(),
        }
        score     = round(sum(components[k] * WEIGHTS[k] for k in components))
        label     = get_label(score)
        direction = "UP" if price >= ref_price else "DOWN"

        print(
            f"  ${price:>10,.2f}  |  Score: {score:3d}/100  {label}"
            f"  [RSI:{components['rsi']:.0f} "
            f"EMA:{components['ema_trend']:.0f} "
            f"Mom:{components['momentum']:.0f} "
            f"Vol:{components['volume']:.0f}]"
        )

        self._log_signal(direction, price, score, components)
        self.notifier.send(score, components, price, self.inr_rate)
        self.current_vol = 0.0


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────

engine = SignalEngine()


def on_open(ws):
    print("🔌 Connected to Bybit WebSocket\n")
    ws.send(json.dumps({"op": "subscribe", "args": [f"publicTrade.{SYMBOL}"]}))


def on_message(ws, message):
    data = json.loads(message)
    if data.get("topic") != f"publicTrade.{SYMBOL}":
        return
    for trade in data.get("data", []):
        engine.on_trade(float(trade["p"]), float(trade["v"]))


def on_error(ws, error):
    print(f"❌ WS Error: {error}")


def on_close(ws, *_):
    print("🔌 WS closed — reconnecting in 5s...")
    time.sleep(5)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("─" * 55)
    print("  BTC Terminal v2 — Weighted Signal Engine")
    print("─" * 55)
    while True:
        ws = websocket.WebSocketApp(
            BYBIT_WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever()
