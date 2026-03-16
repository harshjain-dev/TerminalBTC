"""
Telegram Command Bot — OpenClaw skill interface for BTC Terminal.

Runs alongside BTC.py. Polls for incoming commands and dispatches to skills.

Commands:
  /status    — Live BTC price + current weighted score breakdown
  /research  — Trigger a full research cycle (LLM generates + backtests variants)
  /report    — Performance summary of deployed strategy
  /strategy  — Show current strategy params and version

Run:
    source venv/bin/activate
    python telegram_bot.py
"""

import json
import os
import time
import threading
import requests
from datetime import datetime, timezone

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
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["CHAT_ID"]
STRATEGY_FILE   = "strategy.json"
DEPLOYMENTS_FILE = "deployments.json"
DB_URL          = "http://localhost:9000/exec"
BYBIT_PRICE_URL = "https://api.bybit.com/v5/market/tickers"
GROQ_KEY_FILE   = ".env"

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────

def send(text):
    try:
        requests.post(
            f"{BASE_URL}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        print(f"❌ Send failed: {e}")


def get_updates(offset):
    try:
        r = requests.get(
            f"{BASE_URL}/getUpdates",
            params={"timeout": 20, "offset": offset},
            timeout=25,
        )
        return r.json().get("result", [])
    except Exception:
        return []


# ─── SKILLS ───────────────────────────────────────────────────────────────────

def skill_status():
    """Fetch live BTC price + current score from btc_signals."""
    # Live price from Bybit
    try:
        r = requests.get(
            BYBIT_PRICE_URL,
            params={"category": "linear", "symbol": "BTCUSDT"},
            timeout=5,
        )
        price = float(r.json()["result"]["list"][0]["lastPrice"])
    except Exception:
        price = None

    # Latest signal from DB
    try:
        r = requests.get(
            DB_URL,
            params={"query": "SELECT * FROM btc_signals ORDER BY ts DESC LIMIT 1"},
            timeout=5,
        )
        rows = r.json().get("dataset", [])
        if rows:
            row = rows[0]
            ts, direction, sig_price, inr_rate, score = row[0], row[1], row[2], row[3], row[4]
            s_mom, s_rsi, s_ema, s_vol, s_acc = row[5], row[6], row[7], row[8], row[9]

            label_map = [(80,"🚀 STRONG BUY"),(60,"📈 LEAN BUY"),(40,"⚖️ NEUTRAL"),(20,"📉 LEAN SELL"),(0,"🔻 STRONG SELL")]
            label = next(l for t, l in label_map if score >= t)

            age_secs = int((datetime.now(timezone.utc) -
                           datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())

            lines = [
                f"📡 BTC Status",
                f"{'─'*28}",
            ]
            if price:
                lines.append(f"Live Price : ${price:,.2f}  |  ₹{price * inr_rate:,.2f}")
            lines += [
                f"Last Score : {score}/100  {label}",
                f"Signal Age : {age_secs}s ago",
                f"",
                f"Breakdown:",
                f"  Momentum   {s_mom:.1f}/100",
                f"  RSI        {s_rsi:.1f}/100",
                f"  EMA Trend  {s_ema:.1f}/100",
                f"  Volume     {s_vol:.1f}/100",
                f"  Accuracy   {s_acc:.1f}/100",
            ]
            return "\n".join(lines)
        else:
            return f"⚠️ No signals recorded yet.\nLive price: ${price:,.2f}" if price else "⚠️ No data yet. Is BTC.py running?"
    except Exception as e:
        return f"❌ DB error: {e}"


def skill_strategy():
    """Show current deployed strategy."""
    try:
        with open(STRATEGY_FILE) as f:
            s = json.load(f)

        w = s["weights"]
        p = s["params"]
        perf = s["performance"]

        lines = [
            f"⚙️  Strategy v{s['version']}",
            f"{'─'*28}",
            f"Deployed   : {s['deployed_at'][:10]}",
            f"Description: {s.get('description', 'N/A')}",
            f"",
            f"Weights:",
            f"  Momentum   {w['momentum']:.0%}",
            f"  RSI        {w['rsi']:.0%}",
            f"  EMA Trend  {w['ema_trend']:.0%}",
            f"  Volume     {w['volume']:.0%}",
            f"  Accuracy   {w['accuracy']:.0%}",
            f"",
            f"Params:",
            f"  RSI period     {p['rsi_period']}",
            f"  EMA fast/slow  {p['ema_fast']}/{p['ema_slow']}",
            f"  Buy threshold  {p['alert_buy_threshold']}",
            f"  Sell threshold {p['alert_sell_threshold']}",
            f"",
            f"Backtest:",
            f"  Win rate  {perf['backtest_win_rate'] or 'N/A'}",
            f"  Sharpe    {perf['backtest_sharpe'] or 'N/A'}",
            f"  Signals   {perf['backtest_signals'] or 'N/A'}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Could not load strategy: {e}"


def skill_report():
    """Performance report from live btc_signals data."""
    try:
        # Total signals
        r = requests.get(DB_URL, params={"query": "SELECT count() FROM btc_signals"}, timeout=5)
        total = r.json()["dataset"][0][0]

        # Win rate: check if price went up after BUY signal (4h lookahead)
        query = (
            "SELECT s1.direction, s1.price, s2.price "
            "FROM btc_signals s1 "
            "JOIN btc_signals s2 ON s2.ts > s1.ts "
            "WHERE s1.score >= 70 OR s1.score <= 30 "
            "LIMIT 50"
        )
        r2 = requests.get(DB_URL, params={"query": query}, timeout=5)
        rows = r2.json().get("dataset", [])

        # Score distribution
        dist_query = (
            "SELECT "
            "  sum(CASE WHEN score >= 80 THEN 1 ELSE 0 END) as strong_buy, "
            "  sum(CASE WHEN score >= 60 AND score < 80 THEN 1 ELSE 0 END) as lean_buy, "
            "  sum(CASE WHEN score >= 40 AND score < 60 THEN 1 ELSE 0 END) as neutral, "
            "  sum(CASE WHEN score >= 20 AND score < 40 THEN 1 ELSE 0 END) as lean_sell, "
            "  sum(CASE WHEN score < 20 THEN 1 ELSE 0 END) as strong_sell "
            "FROM btc_signals"
        )
        r3 = requests.get(DB_URL, params={"query": dist_query}, timeout=5)
        dist = r3.json()["dataset"][0] if r3.json().get("dataset") else [0]*5

        with open(STRATEGY_FILE) as f:
            s = json.load(f)

        lines = [
            f"📊 Performance Report",
            f"{'─'*28}",
            f"Strategy   : v{s['version']}",
            f"Total live signals: {total}",
            f"",
            f"Score Distribution:",
            f"  🚀 Strong Buy  (≥80): {dist[0]}",
            f"  📈 Lean Buy  (60-79): {dist[1]}",
            f"  ⚖️  Neutral   (40-59): {dist[2]}",
            f"  📉 Lean Sell (20-39): {dist[3]}",
            f"  🔻 Strong Sell (<20): {dist[4]}",
            f"",
            f"Backtest (at deploy):",
            f"  Win rate : {s['performance']['backtest_win_rate'] or 'N/A'}",
            f"  Sharpe   : {s['performance']['backtest_sharpe'] or 'N/A'}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Report error: {e}"


def skill_research():
    """Run a full research cycle. Runs in background, sends result when done."""
    send("🔬 Starting research cycle...\nThis takes ~60s. I'll message you when done.")

    def run():
        try:
            # Load Groq key from .env if present
            groq_key = os.environ.get("GROQ_API_KEY")
            if not groq_key and os.path.exists(GROQ_KEY_FILE):
                for line in open(GROQ_KEY_FILE):
                    if line.startswith("GROQ_API_KEY="):
                        groq_key = line.strip().split("=", 1)[1]

            if not groq_key:
                send("❌ GROQ_API_KEY not set.\nAdd it to .env:\n  GROQ_API_KEY=your_key")
                return

            # Capture research output
            import io, sys
            from skills.research_agent import run_research_cycle

            old_stdout = sys.stdout
            sys.stdout = buf = io.StringIO()
            try:
                run_research_cycle(api_key=groq_key)
            finally:
                sys.stdout = old_stdout

            output = buf.getvalue()

            # Extract key lines to summarise
            lines = [l for l in output.splitlines() if any(
                kw in l for kw in ["Current:", "Best:", "Deployed", "deploying", "improvement", "win=", "cycles:"]
            )]
            summary = "\n".join(lines[:15]) if lines else output[:500]

            send(f"✅ Research cycle complete:\n\n{summary}")
        except Exception as e:
            send(f"❌ Research failed: {e}")

    threading.Thread(target=run, daemon=True).start()


# ─── NIGHTLY SCHEDULER ───────────────────────────────────────────────────────

RESEARCH_HOUR_UTC = 2   # Run auto-research at 2 AM UTC every night

def _scheduler_loop():
    """Background thread: runs a research cycle once per day at RESEARCH_HOUR_UTC."""
    last_run_day = None
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == RESEARCH_HOUR_UTC and now.day != last_run_day:
            last_run_day = now.day
            send(f"🌙 Nightly research cycle starting (auto-scheduled {RESEARCH_HOUR_UTC:02d}:00 UTC)...")
            skill_research()
        time.sleep(60)  # Check every minute


# ─── COMMAND ROUTER ───────────────────────────────────────────────────────────

COMMANDS = {
    "/status":   skill_status,
    "/strategy": skill_strategy,
    "/report":   skill_report,
}

HELP_TEXT = """🤖 BTC Terminal Commands

/status    — Live price + current signal score
/strategy  — Current deployed strategy params
/report    — Performance report & score distribution
/research  — Run LLM research cycle (finds better strategy)
/help      — Show this message"""


def handle(text):
    cmd = text.strip().split()[0].lower()
    if cmd == "/help":
        return HELP_TEXT
    if cmd == "/research":
        skill_research()
        return None  # Response sent async
    if cmd in COMMANDS:
        return COMMANDS[cmd]()
    return f"Unknown command: {cmd}\n\nType /help for available commands."


# ─── POLLING LOOP ─────────────────────────────────────────────────────────────

def main():
    print("─" * 50)
    print("  BTC Telegram Bot — listening for commands")
    print("─" * 50)
    print(f"  Allowed chat: {CHAT_ID}")
    print(f"  Commands: /status /strategy /report /research /help\n")

    send("🟢 BTC Terminal bot started.\nType /help to see available commands.")

    # Start nightly auto-research scheduler
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print(f"  Auto-research: every night at {RESEARCH_HOUR_UTC:02d}:00 UTC\n")

    offset = 0
    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text    = msg.get("text", "")

            if not text or not text.startswith("/"):
                continue

            # Only respond to the authorised chat
            if chat_id != CHAT_ID:
                print(f"⚠️  Ignored message from unknown chat: {chat_id}")
                continue

            print(f"📨 Command: {text}")
            response = handle(text)
            if response:
                send(response)

        time.sleep(1)


if __name__ == "__main__":
    main()
