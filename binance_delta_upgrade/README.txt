Upgraded Binance Delta Predictor
--------------------------------
Files:
- app.py : Flask app and websocket listener. Edit SYMBOLS and DELTA_THRESHOLD at top.
- templates/index.html : Frontend UI (auto-refresh status).
- static/style.css : Styling.

How it works:
- Connects to Binance combined trade websocket for symbols in SYMBOLS.
- Aggregates trades per 1-minute bucket and computes delta = aggressive_buy - aggressive_sell.
- If delta/total_volume >= DELTA_THRESHOLD => CALL; <= -DELTA_THRESHOLD => PUT; else NO TRADE.
- Prediction for minute T is generated immediately after minute T completes, then evaluated after minute T+1 completes by comparing open/close in that next minute.

Run:
1) pip install flask websocket-client
2) python app.py
3) Open http://127.0.0.1:5000

Notes:
- For production use, run on a VPS, add logging to disk, persistent DB for predictions, and backtest historic trade data.
- To change symbols, edit SYMBOLS variable in app.py (restart required).
- Tune DELTA_THRESHOLD after backtesting.
