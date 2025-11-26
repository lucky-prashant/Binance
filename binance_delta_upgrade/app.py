# binance_delta_upgrade/app.py
# Upgraded Binance delta predictor
# Features:
# - Connects to Binance combined trade stream for multiple symbols
# - Aggregates trades into 1-minute buckets per symbol
# - Generates CALL/PUT/NO TRADE signals every completed minute based on delta fraction
# - Automatically evaluates prediction using next candle and updates stats (wins/losses)
# - Frontend auto-refreshes status every 2 seconds
# Requirements: pip install flask websocket-client

import json
import time
import threading
from collections import deque, defaultdict
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify
import websocket

# ---------------- CONFIG ----------------
SYMBOLS = ["btcusdt"]  # default symbols to monitor (lowercase). Change or add e.g. 'ethusdt'
TIMEFRAME_SECONDS = 60
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams=" + "/".join(f"{s}@trade" for s in SYMBOLS)
DELTA_THRESHOLD = 0.30  # fraction of volume (30% default) to consider strong delta -> tune after backtest
# ----------------------------------------

app = Flask(__name__)

# In-memory stores
trades_lock = threading.Lock()
# per-symbol deque of recent trades (keep 5 minutes)
recent_trades = {s: deque(maxlen=20000) for s in SYMBOLS}

# predictions awaiting evaluation: list of dicts with keys: symbol, predict_time_ms, signal, open, expected_window_end_ms
pending_preds = []
preds_lock = threading.Lock()

# stats
stats = {s.upper(): {"signals":0, "wins":0, "losses":0} for s in SYMBOLS}

# websocket app
ws_app = None
stop_ws = threading.Event()

# ---------------- WebSocket callbacks ----------------
def on_message(ws, message):
    # message contains {"stream":"btcusdt@trade","data":{...trade...}}
    try:
        payload = json.loads(message)
        stream = payload.get("stream","")
        data = payload.get("data") or payload  # sometimes raw trade
        # if stream exists, parse symbol from it
        if stream:
            sym = stream.split("@")[0]
        else:
            sym = data.get("s","").lower()
        if sym not in recent_trades:
            # ignore trades for symbols we don't track
            return
        trade = {
            "time": int(data.get("T", int(time.time()*1000))),
            "price": float(data.get("p",0.0)),
            "qty": float(data.get("q",0.0)),
            "isBuyerMaker": bool(data.get("m", False)),
        }
        with trades_lock:
            recent_trades[sym].append(trade)
    except Exception as e:
        print("on_message parse error:", e)

def on_error(ws, error):
    print("WS error:", error)

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed:", close_status_code, close_msg)

def on_open(ws):
    print("Connected to Binance combined trade stream for:", ",".join([s.upper() for s in SYMBOLS]))

def start_ws():
    global ws_app
    while not stop_ws.is_set():
        try:
            ws_app = websocket.WebSocketApp(
                BINANCE_WS,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("WebSocket crashed, restarting in 2s:", e)
            time.sleep(2)

# ---------------- Aggregation & prediction ----------------
def aggregate_minute_for_symbol(sym, end_ms=None):
    """Aggregate trades in the last TIMEFRAME_SECONDS ending at end_ms (ms). If end_ms None, use now."""
    if end_ms is None:
        end_ms = int(time.time()*1000)
    start_ms = end_ms - TIMEFRAME_SECONDS*1000
    with trades_lock:
        trades = [t for t in recent_trades[sym] if start_ms <= t["time"] < end_ms]
    if not trades:
        return None
    open_p = trades[0]["price"]
    close_p = trades[-1]["price"]
    high_p = max(t["price"] for t in trades)
    low_p = min(t["price"] for t in trades)
    aggressive_buy = sum(t["qty"] for t in trades if t["isBuyerMaker"] is False)
    aggressive_sell = sum(t["qty"] for t in trades if t["isBuyerMaker"] is True)
    delta = aggressive_buy - aggressive_sell
    total = aggressive_buy + aggressive_sell if (aggressive_buy + aggressive_sell) > 0 else 1e-9
    return {
        "open": open_p, "high": high_p, "low": low_p, "close": close_p,
        "delta": delta, "aggressive_buy": aggressive_buy, "aggressive_sell": aggressive_sell,
        "total_volume": total, "start_ms": start_ms, "end_ms": end_ms
    }

def generate_signal(agg):
    if agg is None:
        return "NO TRADE", "no trades"
    frac = agg["delta"] / agg["total_volume"]
    if frac >= DELTA_THRESHOLD:
        return "CALL", f"positive delta strong ({frac*100:.1f}% of vol)"
    elif frac <= -DELTA_THRESHOLD:
        return "PUT", f"negative delta strong ({frac*100:.1f}% of vol)"
    else:
        return "NO TRADE", f"delta weak ({frac*100:.1f}% of vol)"

def monitor_loop():
    """
    Main loop: every second check if a minute just completed.
    When minute completes for a symbol, aggregate its last minute, generate signal,
    and schedule evaluation after next minute completes.
    """
    last_min_seen = {}
    while not stop_ws.is_set():
        now = int(time.time()*1000)
        current_min = (now // 60000)
        for sym in SYMBOLS:
            # determine the minute key based on latest trades available; use system clock minute
            if sym not in last_min_seen:
                last_min_seen[sym] = current_min
            if current_min > last_min_seen[sym]:
                # previous minute completed at end_ms = current_min*60000
                completed_end_ms = current_min * 60000
                agg = aggregate_minute_for_symbol(sym, end_ms=completed_end_ms)
                signal, reason = generate_signal(agg)
                # record prediction to pending_preds for evaluation after next minute
                pred = {
                    "symbol": sym,
                    "predict_time_ms": completed_end_ms,
                    "signal": signal,
                    "reason": reason,
                    "agg": agg,
                    "evaluated": False
                }
                with preds_lock:
                    pending_preds.append(pred)
                if signal in ("CALL","PUT"):
                    stats[sym.upper()]["signals"] += 1
                print(f"[{sym.upper()}] Predicted {signal} at {datetime.fromtimestamp(completed_end_ms/1000)} reason: {reason}")
                last_min_seen[sym] = current_min
        # evaluate any pending predictions where next minute has completed
        with preds_lock:
            to_eval = [p for p in pending_preds if (not p["evaluated"]) and (int(time.time()*1000) >= p["predict_time_ms"] + TIMEFRAME_SECONDS*1000)]
        for p in to_eval:
            # evaluate using candle that spans predict_time_ms -> predict_time_ms + TIMEFRAME_SECONDS*1000
            eval_start = p["predict_time_ms"]
            eval_end = eval_start + TIMEFRAME_SECONDS*1000
            # aggregate trades for evaluation window
            with trades_lock:
                trades = [t for t in recent_trades[p["symbol"]] if eval_start <= t["time"] < eval_end]
            if not trades:
                result = "no data"
                win = False
            else:
                open_p = trades[0]["price"]
                close_p = trades[-1]["price"]
                if close_p > open_p:
                    actual = "CALL"
                elif close_p < open_p:
                    actual = "PUT"
                else:
                    actual = "NO_MOVE"
                win = (p["signal"] == actual)
                result = actual
            # update stats
            if p["signal"] in ("CALL","PUT"):
                if win:
                    stats[p["symbol"].upper()]["wins"] += 1
                else:
                    stats[p["symbol"].upper()]["losses"] += 1
            p["evaluated"] = True
            p["eval_result"] = result
            p["win"] = bool(win)
            p["eval_open_close"] = (open_p if trades else None, close_p if trades else None)
            print(f"Evaluated {p['symbol'].upper()} pred {p['signal']} -> actual {result} (win={win})")
        time.sleep(1)

# ---------------- Flask routes ----------------
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html', symbols=[s.upper() for s in SYMBOLS])

@app.route('/status')
def status():
    # return latest aggregates and pending predictions and stats
    payload = {"symbols": {}, "pending": [], "stats": stats}
    for s in SYMBOLS:
        agg = aggregate_minute_for_symbol(s)
        payload["symbols"][s.upper()] = agg
    with preds_lock:
        payload["pending"] = list(reversed(pending_preds))[-50:]  # return latest 50
    return jsonify(payload)

@app.route('/set_symbols', methods=['POST'])
def set_symbols():
    return jsonify({"ok": False, "msg": "Editing monitored symbols at runtime is not implemented in this demo. Change SYMBOLS variable in app.py and restart."}), 400

# ---------------- Start threads and app ----------------
def ensure_ws_running():
    t = threading.Thread(target=start_ws, daemon=True)
    t.start()
    monitor = threading.Thread(target=monitor_loop, daemon=True)
    monitor.start()

import atexit
def shutdown():
    stop_ws.set()
    try:
        if ws_app:
            ws_app.close()
    except Exception:
        pass
atexit.register(shutdown)

if __name__ == '__main__':
    print("Starting upgraded Binance delta predictor for symbols:", [s.upper() for s in SYMBOLS])
    ensure_ws_running()
    # allow some time for warmup
    time.sleep(2)
    app.run(host='0.0.0.0', port=5000)
