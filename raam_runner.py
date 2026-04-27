"""
raam_runner.py — Lightweight RAAM weekly execution script
Runs in GitHub Actions every Friday at 3:30 PM IST.
No vectorbt. No heavy ML. Just pandas + yfinance + requests.

Reads:  data/stage_3/master_ensemble_signals.csv  (pre-computed, lives in repo)
        data/stage_3/trade_log.csv                 (running trade history)
Writes: data/stage_3/trade_log.csv                 (appends new trades)
        dashboard_data.json                        (consumed by GitHub Pages dashboard)
"""

import os, json, math, requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import date, datetime

# ── Config ─────────────────────────────────────────────────────────────────────
UPSTOX_TOKEN    = os.environ["UPSTOX_TOKEN"]
PORTFOLIO_VALUE = float(os.environ.get("PORTFOLIO_VALUE", "60000"))
MODE            = os.environ.get("MODE", "sandbox")
BINANCE_API     = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET  = os.environ.get("BINANCE_SECRET_KEY", "")

UPSTOX_BASE     = "https://api-sandbox.upstox.com" if MODE == "sandbox" else "https://api-hft.upstox.com"

INSTRUMENT_MAP = {
    "NIFTYBEES.NS"  : "NSE_EQ|INF204KB14I2",
    "JUNIORBEES.NS" : "NSE_EQ|INF732E01045",
    "GOLDBEES.NS"   : "NSE_EQ|INF204KB17I5",
    "BANKBEES.NS"   : "NSE_EQ|INF204KB15I9",
    "LIQUIDBEES.NS" : "NSE_EQ|INF732E01037",
    "ITBEES.NS"     : "NSE_EQ|INF204KB15V2",
    "PHARMABEES.NS" : "NSE_EQ|INF204KC1089",
    "INFRABEES.NS"  : "NSE_EQ|INF732E01268",
    "AUTOBEES.NS"   : "NSE_EQ|INF204KC1337",
    "CPSEETF.NS"    : "NSE_EQ|INF457M01133",
    "HNGSNGBEES.NS" : "NSE_EQ|INF204KB19I1",
    "MON100.NS"     : "NSE_EQ|INF247L01AP3",
    "BTC-USD"       : "BINANCE",
}

SIGNAL_FILE     = "data/stage_3/master_ensemble_signals.csv"
TRADE_LOG       = "data/stage_3/trade_log.csv"
DASHBOARD_FILE  = "dashboard_data.json"

MOMENTUM_LOYALTY_BUFFER = 0.02

print(f"{'='*60}")
print(f"RAAM RUNNER | {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} | {MODE.upper()}")
print(f"{'='*60}")

# ── 1. Load signals + prices ───────────────────────────────────────────────────
signals_df   = pd.read_csv(SIGNAL_FILE, index_col="Date", parse_dates=True)
active_tickers = signals_df.columns.tolist()

all_tickers  = active_tickers + ["LIQUIDBEES.NS"]
print("Downloading prices via yfinance...")

# Filter to Nifty trading dates (strips BTC weekends)
raw_closes   = yf.download(all_tickers, period="180d", progress=False)["Close"]
nifty_dates  = raw_closes["NIFTYBEES.NS"].dropna().index
closes       = raw_closes.loc[raw_closes.index.isin(nifty_dates)].ffill()

# ── 2. Align signals & closes ─────────────────────────────────────────────────
aligned = signals_df.join(closes, how="inner", lsuffix="_sig", rsuffix="_px")
signals  = aligned[[c + "_sig" for c in active_tickers]].rename(
    columns={c + "_sig": c for c in active_tickers})
prices   = aligned[closes.columns]

# ── 3. Compute 12W momentum ───────────────────────────────────────────────────
daily_rets = prices.pct_change().fillna(0)
mom_12w    = prices[active_tickers].pct_change(periods=60).fillna(0)

# ── 4. Determine this Friday's signal ────────────────────────────────────────
today_date   = signals.index[-1]  # most recent available trading day
today_sig    = signals.loc[today_date]
uptrend      = today_sig[today_sig == 1.0].index.tolist()
print(f"Uptrend assets ({len(uptrend)}): {uptrend}")

# Load previous top-3 from trade log for hysteresis
current_top3 = []
if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    last_friday_trades = tl[tl["action"] == "BUY"].tail(10)
    if not last_friday_trades.empty:
        current_top3 = last_friday_trades["ticker"].tolist()

# Apply hysteresis loyalty bonus
today_mom = mom_12w.loc[today_date, uptrend].copy() if uptrend else pd.Series(dtype=float)
for asset in current_top3:
    if asset in today_mom.index:
        today_mom[asset] += MOMENTUM_LOYALTY_BUFFER

# Select top-3
if len(uptrend) == 0:
    target_alloc = {"LIQUIDBEES.NS": 1.0}
else:
    top3 = today_mom.nlargest(3).index.tolist()
    n    = len(top3)
    w    = min(1.0 / n, 0.40)
    target_alloc = {t: w for t in top3}
    target_alloc["LIQUIDBEES.NS"] = max(0.0, 1.0 - sum(target_alloc.values()))

print(f"Target allocation: {target_alloc}")

# ── 5. Current prices for share calculation ───────────────────────────────────
def last_price(ticker):
    try:
        if ticker == "BTC-USD":
            btc_usd = float(yf.Ticker("BTC-USD").fast_info.last_price)
            inr     = float(yf.Ticker("INR=X").fast_info.last_price)
            return btc_usd * inr
        p = yf.Ticker(ticker).fast_info
        return float(p.last_price or p.previous_close)
    except:
        return float(closes[ticker].iloc[-1]) if ticker in closes else None

live_prices = {t: last_price(t) for t in target_alloc}
target_shares = {}
for ticker, weight in target_alloc.items():
    p = live_prices.get(ticker)
    if not p: continue
    rupees = weight * PORTFOLIO_VALUE
    qty    = round(rupees / p, 5) if ticker == "BTC-USD" else math.floor(rupees / p)
    target_shares[ticker] = qty

# ── 6. Place orders ───────────────────────────────────────────────────────────
headers = {"Authorization": f"Bearer {UPSTOX_TOKEN}",
           "Content-Type": "application/json", "Accept": "application/json"}

trade_records = []
for ticker, qty in target_shares.items():
    if qty <= 0 or ticker == "BTC-USD":
        continue
    inst = INSTRUMENT_MAP.get(ticker)
    if not inst or inst == "BINANCE":
        continue
    payload = {"quantity": int(qty), "product": "D", "validity": "DAY",
               "price": 0, "tag": "RAAM", "instrument_token": inst,
               "order_type": "MARKET", "transaction_type": "BUY",
               "disclosed_quantity": 0, "trigger_price": 0, "is_amo": True}
    resp = requests.post(f"{UPSTOX_BASE}/v2/order/place",
                         json=payload, headers=headers, timeout=15)
    oid  = resp.json().get("data", {}).get("order_id", "FAILED") if resp.ok else "FAILED"
    print(f"  {'OK' if resp.ok else 'FAIL'} BUY {qty} {ticker} → {oid}")
    trade_records.append({
        "date": date.today().isoformat(), "ticker": ticker,
        "action": "BUY", "quantity": qty,
        "price": live_prices.get(ticker, 0), "order_id": oid, "mode": MODE
    })

# ── 7. Update trade log ───────────────────────────────────────────────────────
if trade_records:
    df_new = pd.DataFrame(trade_records)
    if os.path.exists(TRADE_LOG):
        df_all = pd.concat([pd.read_csv(TRADE_LOG), df_new], ignore_index=True)
    else:
        df_all = df_new
    os.makedirs("data/stage_3", exist_ok=True)
    df_all.to_csv(TRADE_LOG, index=False)
    print(f"Trade log updated → {len(df_all)} total rows")

# ── 8. Build dashboard_data.json ─────────────────────────────────────────────
# Compute P&L from trade log
holdings, invested = {}, {}
if os.path.exists(TRADE_LOG):
    for _, r in pd.read_csv(TRADE_LOG).iterrows():
        t = r["ticker"]
        holdings.setdefault(t, 0); invested.setdefault(t, 0.0)
        if r["action"] == "BUY":
            holdings[t]  += r["quantity"]
            invested[t]  += r["quantity"] * r["price"]
        elif r["action"] == "SELL":
            avg  = invested[t] / holdings[t] if holdings[t] > 0 else 0
            holdings[t]  -= r["quantity"]
            invested[t]  -= r["quantity"] * avg

positions = []
total_inv = total_cur = 0.0
for t, qty in holdings.items():
    if qty < 0.0001: continue
    lp = last_price(t) or 0
    cur = qty * lp
    inv = invested.get(t, 0)
    pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0
    positions.append({"ticker": t, "shares": round(qty, 5),
                       "avg_cost": round(inv/qty, 2) if qty > 0 else 0,
                       "live_price": round(lp, 2),
                       "invested": round(inv, 0), "current": round(cur, 0),
                       "pnl": round(pnl, 0), "pnl_pct": round(pnl_pct, 2)})
    total_inv += inv; total_cur += cur

# Historical equity curve from trade log (simple daily mark-to-market)
net_pnl     = total_cur - total_inv
net_pnl_pct = (net_pnl / total_inv * 100) if total_inv > 0 else 0

# Market breadth (today)
breadth = int(today_sig.sum())

dashboard = {
    "generated_at"   : datetime.utcnow().isoformat() + "Z",
    "mode"           : MODE,
    "portfolio_value": PORTFOLIO_VALUE,
    "total_invested" : round(total_inv, 0),
    "current_value"  : round(total_cur, 0),
    "net_pnl"        : round(net_pnl, 0),
    "net_pnl_pct"    : round(net_pnl_pct, 2),
    "breadth"        : breadth,
    "breadth_max"    : len(active_tickers),
    "target_alloc"   : {k: round(v*100, 1) for k, v in target_alloc.items()},
    "positions"      : positions,
    "next_rebalance" : "Next Friday 3:30 PM IST",
    "last_run_date"  : date.today().isoformat(),
}

with open(DASHBOARD_FILE, "w") as f:
    json.dump(dashboard, f, indent=2)

print(f"\nDashboard data written → {DASHBOARD_FILE}")
print(f"P&L: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%)")
print("DONE.")
