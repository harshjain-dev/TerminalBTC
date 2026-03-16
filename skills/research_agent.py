"""
Research Agent — autoresearch pattern applied to crypto strategy.

Each cycle:
  1. Load current strategy + its backtest performance
  2. Load failure history (what didn't work)
  3. Ask Groq LLM to generate N strategy variants
  4. Backtest each variant
  5. Deploy best if it beats current strategy
  6. Save run history to deployments.json

Usage:
    python skills/research_agent.py
    python skills/research_agent.py --dry-run   # backtest only, no deploy
"""

import json
import os
import sys
import argparse
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from skills.backtest import run_backtest
from groq import Groq

STRATEGY_FILE   = "strategy.json"
DEPLOYMENTS_FILE = "deployments.json"
GROQ_MODEL      = "llama-3.3-70b-versatile"
N_VARIANTS      = 5       # Variants to generate per cycle
MIN_IMPROVEMENT = 0.03    # Must beat current win_rate by this margin to deploy


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def load_strategy():
    with open(STRATEGY_FILE) as f:
        return json.load(f)


def save_strategy(strategy):
    with open(STRATEGY_FILE, "w") as f:
        json.dump(strategy, f, indent=2)


def load_deployments():
    if not os.path.exists(DEPLOYMENTS_FILE):
        return []
    with open(DEPLOYMENTS_FILE) as f:
        return json.load(f)


def save_deployments(history):
    with open(DEPLOYMENTS_FILE, "w") as f:
        json.dump(history, f, indent=2)


def build_failure_context(deployments):
    """Summarise failed deployments for the LLM prompt."""
    if not deployments:
        return "No previous deployments yet."

    lines = []
    for d in deployments[-5:]:  # Last 5 cycles
        status = "✅ DEPLOYED" if d.get("deployed") else "❌ REJECTED"
        result = d.get("best_result", {})
        lines.append(
            f"  [{d['timestamp'][:10]}] {status} "
            f"win_rate={result.get('win_rate','?')} "
            f"sharpe={result.get('sharpe','?')} "
            f"reason={d.get('reason','')}"
        )

    failed = [d for d in deployments if not d.get("deployed")]
    if failed:
        last_failed = failed[-1]
        params = last_failed.get("best_params", {})
        lines.append(f"\nLast rejected params: {json.dumps(params, indent=2)}")

    return "\n".join(lines)


# ─── LLM INTERACTION ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a quantitative crypto strategy researcher.
Your job is to generate improved BTC/USDT signal strategy configurations.

You will be given:
- The current strategy configuration and its backtest performance
- History of past strategies that were tried (and whether they worked)
- Constraints on what parameters are allowed

You must output ONLY a valid JSON array of {N} strategy variants.
No explanation, no markdown, no commentary — just the raw JSON array.

Each variant must follow this exact schema:
[
  {{
    "description": "one-line explanation of what changed and why",
    "weights": {{
      "momentum": <float 0.05–0.50>,
      "rsi": <float 0.05–0.50>,
      "ema_trend": <float 0.05–0.50>,
      "volume": <float 0.05–0.50>,
      "accuracy": <float 0.05–0.30>
    }},
    "params": {{
      "rsi_period": <int 7–21>,
      "ema_fast": <int 5–15>,
      "ema_slow": <int 15–50>,
      "momentum_scale": <float 5–30>,
      "volume_spike_cap": <float 1.0–3.0>,
      "alert_buy_threshold": <int 60–80>,
      "alert_sell_threshold": <int 20–40>,
      "score_interval_secs": 30
    }}
  }}
]

Rules:
- Weights must sum to exactly 1.0
- ema_fast must be less than ema_slow
- Do NOT repeat parameter combinations that already failed
- Aim to improve win_rate above 0.55 and sharpe above 0.1
"""


def ask_llm_for_variants(client, current_strategy, current_result, deployments):
    failure_ctx = build_failure_context(deployments)

    user_msg = f"""
Current strategy:
{json.dumps(current_strategy['weights'], indent=2)}
{json.dumps(current_strategy['params'], indent=2)}

Current backtest performance (lookahead=4h):
  win_rate:      {current_result.get('win_rate', 'N/A')}
  total_signals: {current_result.get('total_signals', 'N/A')}
  avg_pnl_pct:   {current_result.get('avg_pnl_pct', 'N/A')}
  sharpe:        {current_result.get('sharpe', 'N/A')}

Deployment history:
{failure_ctx}

Generate {N_VARIANTS} new strategy variants that are likely to perform better.
Remember: output ONLY the raw JSON array, nothing else.
"""

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(N=N_VARIANTS)},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.7,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw)


# ─── MAIN RESEARCH CYCLE ─────────────────────────────────────────────────────

def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()


def _git_push(strategy, result):
    """Auto-commit strategy.json + deployments.json and push to GitHub."""
    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        print("   ⚠️  GITHUB_PAT not set — skipping auto-push.")
        return

    root = os.path.dirname(os.path.dirname(__file__))
    version = strategy.get("version", "?")
    win_rate = result.get("win_rate", 0)
    msg = (
        f"strategy: auto-deploy v{version} "
        f"(win={win_rate:.0%} sharpe={result.get('sharpe',0):.3f})"
    )

    cmds = [
        ["git", "-C", root, "add", "strategy.json", "deployments.json"],
        ["git", "-C", root, "commit", "-m", msg],
        ["git", "-C", root, "push"],
    ]

    for cmd in cmds:
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if out.returncode != 0 and "nothing to commit" not in out.stdout:
                print(f"   ⚠️  git: {out.stderr.strip()[:120]}")
                return
        except Exception as e:
            print(f"   ⚠️  git push failed: {e}")
            return

    print(f"   📦 Auto-pushed to GitHub: {msg}")


def run_research_cycle(dry_run=False, api_key=None):
    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        print("❌ GROQ_API_KEY not set. Export it or pass --api-key.")
        return

    client      = Groq(api_key=key)
    strategy    = load_strategy()
    deployments = load_deployments()

    print("─" * 55)
    print(f"  Research Cycle — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("─" * 55)

    # Backtest current strategy
    print("\n📊 Backtesting current strategy...")
    current_result = run_backtest(strategy, lookahead_candles=4)
    if "error" in current_result:
        print(f"❌ Current backtest failed: {current_result['error']}")
        return

    current_win_rate = current_result["win_rate"]
    current_sharpe   = current_result["sharpe"]
    print(f"   Current: win_rate={current_win_rate:.2%}  sharpe={current_sharpe:.3f}  signals={current_result['total_signals']}")

    # Ask LLM for variants
    print(f"\n🤖 Asking LLM ({GROQ_MODEL}) for {N_VARIANTS} variants...")
    try:
        variants = ask_llm_for_variants(client, strategy, current_result, deployments)
        print(f"   Got {len(variants)} variants.")
    except Exception as e:
        print(f"❌ LLM failed: {e}")
        return

    # Backtest each variant
    print("\n🔬 Backtesting variants:")
    results = []
    for i, variant in enumerate(variants):
        candidate = {
            "version": strategy["version"] + 1,
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "description": variant.get("description", ""),
            "weights": variant["weights"],
            "params": variant["params"],
            "performance": {}
        }
        try:
            result = run_backtest(candidate, lookahead_candles=4)
            if "error" in result:
                print(f"   [{i+1}] ❌ {result['error']}")
                continue
            results.append((candidate, result))
            status = "✅" if result["win_rate"] > current_win_rate else "  "
            print(
                f"   [{i+1}] {status} win={result['win_rate']:.2%}  "
                f"sharpe={result['sharpe']:.3f}  signals={result['total_signals']}"
                f"  — {variant.get('description', '')[:60]}"
            )
        except Exception as e:
            print(f"   [{i+1}] ❌ exception: {e}")

    if not results:
        print("\n⚠️  No valid variants produced.")
        return

    # Pick best
    best_strategy, best_result = max(results, key=lambda x: (x[1]["win_rate"], x[1]["sharpe"]))
    print(f"\n🏆 Best: win_rate={best_result['win_rate']:.2%}  sharpe={best_result['sharpe']:.3f}")

    # Decide whether to deploy
    improvement = best_result["win_rate"] - current_win_rate
    should_deploy = (
        improvement >= MIN_IMPROVEMENT and
        best_result["win_rate"] > 0.50 and
        not dry_run
    )

    deploy_entry = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "deployed":    should_deploy,
        "reason":      None,
        "best_result": best_result,
        "best_params": {
            "weights": best_strategy["weights"],
            "params":  best_strategy["params"],
        },
        "current_win_rate": current_win_rate,
    }

    if should_deploy:
        best_strategy["performance"] = {
            "backtest_win_rate":  best_result["win_rate"],
            "backtest_signals":   best_result["total_signals"],
            "backtest_sharpe":    best_result["sharpe"],
            "live_win_rate":      None,
            "live_signals":       0,
            "failure_reason":     None,
        }
        save_strategy(best_strategy)
        deploy_entry["reason"] = f"Improved win_rate by {improvement:.2%}"
        print(f"\n🚀 Deployed! New strategy saved to strategy.json")
        print(f"   Improvement: +{improvement:.2%} win rate")
        _git_push(best_strategy, best_result)
    elif dry_run:
        deploy_entry["reason"] = "dry-run mode"
        print(f"\n🔍 Dry run — not deploying. Best improvement: {improvement:+.2%}")
    else:
        deploy_entry["reason"] = (
            f"Insufficient improvement: {improvement:+.2%} (need ≥{MIN_IMPROVEMENT:.0%})"
            if improvement < MIN_IMPROVEMENT
            else f"Win rate {best_result['win_rate']:.2%} still below 50%"
        )
        print(f"\n⏭️  Not deploying: {deploy_entry['reason']}")
        print("   LLM will receive this feedback in the next cycle.")

    # Save deployment history
    deployments.append(deploy_entry)
    save_deployments(deployments)
    print(f"\n📝 Cycle saved to deployments.json (total cycles: {len(deployments)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true", help="Backtest only, don't save strategy.json")
    parser.add_argument("--api-key",  type=str, help="Groq API key (or set GROQ_API_KEY env var)")
    args = parser.parse_args()
    run_research_cycle(dry_run=args.dry_run, api_key=args.api_key)
