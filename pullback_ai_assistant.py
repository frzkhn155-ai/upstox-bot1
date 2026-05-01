"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   PULLBACK AI ASSISTANT  —  Tuned for cacheheadlesspullback_FIXED.py       ║
║                                                                              ║
║   Strategies covered:                                                        ║
║     • PULLBACK_CE  — 5-min pullback CE, gated by 60-min EMA20+RSI14        ║
║     • S3           — S3 support breakdown PE (bearish)                       ║
║     • ORB          — Opening Range Breakout (CE or PE)                       ║
║                                                                              ║
║   AI Providers: Groq (primary, multi-key failover) → NVIDIA (fallback)      ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os as _os
import threading
import time
import json
import requests as _req
from datetime import datetime
from typing import Optional, Callable, Dict, Any

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

GROQ_KEYS = [k for k in [
    _os.environ.get("GROQ_API_KEY", ""),
    _os.environ.get("GROQ_API_KEY_2", ""),
] if k and "YOUR_GROQ" not in k]

NVIDIA_API_KEY = _os.environ.get("NVIDIA_API_KEY", "YOUR_NVIDIA_API_KEY_HERE")
AI_ENABLED = True

# -- Models --
GROQ_MODEL   = "llama-3.3-70b-versatile"
NVIDIA_MODEL = "meta/llama-3.3-70b-instruct"

# -- Behaviour --
AI_SCAN_INTERVAL_SECONDS    = 35
AI_MAX_TOKENS               = 500
AI_TEMPERATURE              = 0.2
AI_AUTO_EXIT_ENABLED        = True
AI_MIN_HOLD_MINUTES         = 15
AI_LOSS_EXIT_THRESHOLD_PCT  = 5.0
AI_SILENT_WHEN_NO_POSITIONS = True

# -- Soft trailing exit --
AI_SOFT_EXIT_ENABLED  = True
AI_TRAIL_TRIGGER_PCT  = 15.0   # start trailing after 15% profit
AI_TRAIL_TIGHT_PCT    = 3.0    # exit if pulls back 3% from peak
AI_FORCE_EXIT_PCT     = 25.0   # force exit at 25% profit regardless

# -- Failover --
GROQ_RETRY_SECONDS = 300
_GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
_NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# -- Internal state --
_ai_thread:            Optional[threading.Thread] = None
_ai_call_count:        int = 0
_groq_call_count:      int = 0
_nvidia_call_count:    int = 0
_current_groq_key_idx: int = 0
_using_nvidia:         bool = False
_groq_failed_at:       Optional[datetime] = None

_bot_globals_getter: Optional[Callable[[], Dict[str, Any]]] = None
_trader_getter:      Optional[Callable[[], Any]] = None

# ── System prompt — covers all 3 strategies ──────────────────────────────────
_SYSTEM_PROMPT = """You are a real-time trading assistant for an Upstox F&O options bot.

This bot runs THREE strategies simultaneously:

1. PULLBACK_CE (Bullish):
   - Buys CE options on 5-min RSI(2) pullback setups.
   - Entry requires: price > EMA200, 5-min RSI(2) oversold, 60-min EMA20 upslope + RSI(14) > 50.
   - EXIT signals: price drops back below EMA200 on 5-min chart, or RSI(2) > 85 (overbought exhaustion),
     or Heikin-Ashi candle flips red AND Klinger Volume Oscillator confirms reversal.

2. S3 (Bearish — PE options):
   - Buys PE options on confirmed S3 support breakdown.
   - Entry requires: price breaks S3 level with volume surge and Klinger bearish confirmation.
   - EXIT signals: price recovers back above S3, or Heikin-Ashi flips green AND Klinger turns bullish,
     or RSI(2) < 10 (oversold exhaustion on downside).

3. ORB (Opening Range Breakout — CE or PE):
   - Trades breakout of the first 30-minute opening range.
   - CE for upside breakout, PE for downside breakdown.
   - EXIT signals: price re-enters the opening range, or reverses through ORB stop level.

General rules for ALL strategies:
- EXIT only on confirmed reversal signals (HA flip + Klinger confirmation).
- Minimum hold: 15 minutes (unless loss > 5% or profit > 25%).
- Do NOT exit profitable positions prematurely — respect trailing stop logic.
- Consider strategy direction: PULLBACK_CE and ORB-CE are bullish (price rising = good),
  S3 and ORB-PE are bearish (price falling = good).

Output format (exactly):
SUMMARY: [brief market commentary — mention index levels if available]
POSITIONS: [SYMBOL] ([STRATEGY]): [ACTION] [REASON]
END."""

# ══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_market_context(trader) -> Dict[str, Any]:
    context = {}
    if not trader:
        return context
    try:
        nifty  = trader.get_ltp("NSE_INDEX|Nifty 50")
        bnifty = trader.get_ltp("NSE_INDEX|Nifty Bank")
        if nifty:  context["nifty"]      = nifty
        if bnifty: context["bank_nifty"] = bnifty
    except Exception:
        pass
    return context

def _build_snapshot(globals_dict: Dict[str, Any], trader=None) -> Dict[str, Any]:
    snapshot = {
        "time":           datetime.now().strftime("%H:%M:%S"),
        "market_context": _fetch_market_context(trader),
        "positions":      [],
        "recent_trades":  [],
        "daily_pnl":      globals_dict.get("DAILY_PNL", 0.0),
        "order_counts": {
            # All directional entries (PULLBACK_CE + S3 PE + R3 CE) share DAILY_ORDER_COUNT
            "directional_total": globals_dict.get("DAILY_ORDER_COUNT", 0),
            "orb":               globals_dict.get("ORB_ORDER_COUNT", 0),
        },
        "hourly_cache_entries": len(globals_dict.get("hourly_trend_cache", {})),
    }

    active = globals_dict.get("ACTIVE_POSITIONS", {})
    for pos_id, pos in active.items():
        strategy   = pos.get("strategy", "UNKNOWN")
        option_type = pos.get("option_type", "CE")      # CE or PE
        entry      = pos.get("entry_price", 0)
        current    = pos.get("current_price", entry)

        # For PE: profit when price falls, so invert pnl direction for underlying
        # Option PnL is always (current - entry) / entry — option price should rise for both
        pnl_pct = ((current - entry) / entry * 100) if entry else 0

        hold_min = round(
            (datetime.now() - pos.get("timestamp", datetime.now())).total_seconds() / 60, 1
        )

        position_entry = {
            "symbol":            pos.get("symbol"),
            "strategy":          strategy,
            "option_type":       option_type,           # CE or PE
            "entry":             round(entry, 2),
            "current":           round(current, 2),
            "pnl_pct":           round(pnl_pct, 2),     # option premium PnL %
            "hold_time_min":     hold_min,
            "klinger_confirmed": pos.get("klinger_confirmed", False),
        }

        # PULLBACK_CE specific fields
        if strategy == "PULLBACK_CE":
            position_entry.update({
                "ema_200_at_entry":  pos.get("ema_200_at_entry"),
                "pullback_rsi_2":    pos.get("pullback_rsi_2"),
                "vwap_at_entry":     pos.get("vwap_at_entry"),
                "underlying_sl":     pos.get("underlying_stop_loss"),
                "underlying_target": pos.get("underlying_target"),
            })

        # S3 specific fields
        elif strategy == "S3":
            position_entry.update({
                "underlying_sl":       pos.get("underlying_stop_loss"),
                "underlying_target":   pos.get("underlying_target"),
                "signal_entry_price":  pos.get("signal_entry_price"),
            })

        # ORB specific fields
        elif strategy == "ORB":
            position_entry.update({
                "underlying_sl":  pos.get("underlying_stop_loss"),
                "entry_trigger":  pos.get("entry_trigger"),
            })

        snapshot["positions"].append(position_entry)

    # Last 5 closed trades
    closed = globals_dict.get("CLOSED_POSITIONS", [])
    for trade in closed[-5:]:
        snapshot["recent_trades"].append({
            "symbol":      trade.get("symbol"),
            "strategy":    trade.get("strategy", "UNKNOWN"),
            "option_type": trade.get("option_type", "CE"),
            "pnl_pct":     round(trade.get("pnl_percent", 0), 2),
            "exit_reason": trade.get("exit_reason"),
        })

    return snapshot

# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_response(response: str) -> Dict[str, str]:
    """Parse AI response into {symbol: action} dict."""
    decisions = {}
    in_positions = False
    for line in response.split("\n"):
        if "POSITIONS:" in line:
            in_positions = True
            continue
        if not in_positions:
            continue
        line = line.strip()
        if not line or line == "END.":
            in_positions = False
            continue
        # Format: "SYMBOL (STRATEGY): ACTION reason"
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        # Extract symbol — first word before any parenthesis
        sym = parts[0].strip().split("(")[0].strip().split()[-1]
        content = parts[1].strip().upper()
        if "EXIT"  in content: decisions[sym] = "EXIT"
        elif "WATCH" in content: decisions[sym] = "WATCH"
        else:                    decisions[sym] = "HOLD"
    return decisions

def _print_ai_response(response: str):
    print("\n" + "="*100)
    print("🤖 PULLBACK AI ASSISTANT  (PULLBACK_CE | S3 PE | ORB CE/PE)")
    print("="*100)
    print(response)
    print("="*100 + "\n")

# ══════════════════════════════════════════════════════════════════════════════
# AI PROVIDER ROUTER — Groq primary, NVIDIA fallback
# ══════════════════════════════════════════════════════════════════════════════

def _call_ai(snapshot: dict) -> Optional[str]:
    global _using_nvidia, _groq_failed_at, _ai_call_count, _current_groq_key_idx
    global _groq_call_count, _nvidia_call_count

    # Check for Groq recovery after cooldown
    if _using_nvidia and _groq_failed_at:
        if (datetime.now() - _groq_failed_at).total_seconds() >= GROQ_RETRY_SECONDS:
            _using_nvidia = False

    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": json.dumps(snapshot)},
        ],
        "max_tokens":  AI_MAX_TOKENS,
        "temperature": AI_TEMPERATURE,
    }

    # 1. Try Groq key pool (round-robin)
    if not _using_nvidia and GROQ_KEYS:
        for _ in range(len(GROQ_KEYS)):
            key = GROQ_KEYS[_current_groq_key_idx]
            if not key or "YOUR_GROQ" in key:
                _current_groq_key_idx = (_current_groq_key_idx + 1) % len(GROQ_KEYS)
                continue
            try:
                p    = {**payload, "model": GROQ_MODEL}
                resp = _req.post(
                    _GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json=p, timeout=12,
                )
                if resp.status_code == 200:
                    _groq_call_count += 1
                    _ai_call_count   += 1
                    return resp.json()["choices"][0]["message"]["content"].strip()
                _current_groq_key_idx = (_current_groq_key_idx + 1) % len(GROQ_KEYS)
            except Exception:
                _current_groq_key_idx = (_current_groq_key_idx + 1) % len(GROQ_KEYS)

        # All Groq keys failed — switch to NVIDIA
        _using_nvidia   = True
        _groq_failed_at = datetime.now()
        print("⚠️ All Groq keys failed — switching to NVIDIA fallback")

    # 2. NVIDIA fallback
    if NVIDIA_API_KEY and "YOUR_NVIDIA" not in NVIDIA_API_KEY:
        try:
            p    = {**payload, "model": NVIDIA_MODEL}
            resp = _req.post(
                _NVIDIA_URL,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
                json=p, timeout=20,
            )
            if resp.status_code == 200:
                _nvidia_call_count += 1
                _ai_call_count     += 1
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    return None

# ══════════════════════════════════════════════════════════════════════════════
# EXIT ENFORCEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _enforce_exits(decisions: Dict[str, str], trader, globals_dict: Dict[str, Any]):
    active = globals_dict.get("ACTIVE_POSITIONS", {})

    for sym, action in decisions.items():
        if action != "EXIT":
            continue

        # Find matching position
        pos_id = pos_data = None
        for pid, p in active.items():
            if p.get("symbol") == sym:
                pos_id, pos_data = pid, p
                break
        if not pos_data:
            continue

        strategy    = pos_data.get("strategy", "UNKNOWN")
        option_type = pos_data.get("option_type", "CE")
        entry_time  = pos_data.get("timestamp", datetime.now())
        hold_min    = (datetime.now() - entry_time).total_seconds() / 60
        pnl_pct     = pos_data.get("pnl_percent", 0)

        # Respect minimum hold time (unless hard thresholds breached)
        if hold_min < AI_MIN_HOLD_MINUTES:
            if not (pnl_pct <= -AI_LOSS_EXIT_THRESHOLD_PCT or pnl_pct >= AI_FORCE_EXIT_PCT):
                print(f"🤖 AI wants to exit {sym} ({strategy} {option_type}) "
                      f"but hold time {hold_min:.1f}m < {AI_MIN_HOLD_MINUTES}m minimum — skipping")
                continue

        # Soft trailing — let runners run
        if AI_SOFT_EXIT_ENABLED and pnl_pct > AI_TRAIL_TRIGGER_PCT:
            peak = pos_data.get("ai_peak_pnl", 0.0)
            if pnl_pct > peak:
                pos_data["ai_peak_pnl"] = pnl_pct
                peak = pnl_pct
            pullback_from_peak = peak - pnl_pct
            if pullback_from_peak < AI_TRAIL_TIGHT_PCT:
                print(f"🤖 AI trailing {sym} ({strategy} {option_type}) — "
                      f"peak {peak:.1f}% pullback {pullback_from_peak:.1f}% "
                      f"< {AI_TRAIL_TIGHT_PCT}% threshold — holding")
                continue

        # Execute exit
        print(f"🤖 AI auto-exiting {sym} | {strategy} {option_type} | "
              f"Hold: {hold_min:.1f}m | PnL: {pnl_pct:.1f}%")

        option_key = pos_data.get("instrument_key")
        if option_key and trader:
            ltp = trader.get_ltp(option_key)
            if ltp:
                exit_func = globals_dict.get("exit_position")
                if exit_func:
                    exit_func(trader, pos_id, pos_data, ltp, "AI_RECOMMENDED_EXIT")
                else:
                    print("⚠️ exit_position function not found in globals")
            else:
                print(f"⚠️ Could not fetch LTP for {sym} ({option_key})")
        else:
            print(f"⚠️ No instrument key or trader for {sym}")

# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND THREAD
# ══════════════════════════════════════════════════════════════════════════════

def _ai_loop():
    while AI_ENABLED:
        try:
            if _bot_globals_getter is None or _trader_getter is None:
                time.sleep(5)
                continue

            globals_dict = _bot_globals_getter()
            trader       = _trader_getter()

            active = globals_dict.get("ACTIVE_POSITIONS", {})
            if AI_SILENT_WHEN_NO_POSITIONS and not active:
                time.sleep(AI_SCAN_INTERVAL_SECONDS)
                continue

            snapshot = _build_snapshot(globals_dict, trader)
            response = _call_ai(snapshot)

            if response:
                _print_ai_response(response)
                decisions = _parse_response(response)
                if AI_AUTO_EXIT_ENABLED and trader:
                    _enforce_exits(decisions, trader, globals_dict)
            else:
                print("⚠️ Pullback AI assistant unavailable (no response from Groq or NVIDIA)")

        except Exception as e:
            print(f"⚠️ Pullback AI loop error: {e}")

        time.sleep(AI_SCAN_INTERVAL_SECONDS)

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

def start_ai_assistant(bot_globals_getter: Callable[[], Dict[str, Any]],
                       trader_getter: Callable[[], Any]) -> None:
    global _ai_thread, _bot_globals_getter, _trader_getter
    if not AI_ENABLED:
        print("🤖 Pullback AI Assistant disabled by configuration.")
        return
    if _ai_thread and _ai_thread.is_alive():
        print("🤖 Pullback AI Assistant already running.")
        return
    _bot_globals_getter = bot_globals_getter
    _trader_getter      = trader_getter
    _ai_thread = threading.Thread(target=_ai_loop, daemon=True)
    _ai_thread.start()
    print("🤖 Pullback AI Assistant started (covers PULLBACK_CE | S3 PE | ORB CE/PE).")

def ai_status() -> str:
    if not AI_ENABLED:
        return "🤖 Pullback AI Assistant: DISABLED"
    provider = "NVIDIA (Groq failed)" if _using_nvidia else "Groq"
    return (
        f"🤖 Pullback AI: {len(GROQ_KEYS)} Groq key(s) + NVIDIA fallback"
        f" | Active: {provider}"
        f" | Calls: {_ai_call_count} (Groq: {_groq_call_count}, NVIDIA: {_nvidia_call_count})"
        f" | Strategies: PULLBACK_CE | S3 PE | ORB CE/PE"
    )

if __name__ == "__main__":
    print("Pullback AI Assistant — import and call start_ai_assistant()")
