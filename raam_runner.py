"""
raam_runner.py  —  RAAM Weekly Execution Engine
Runs in GitHub Actions every Friday 3:30 PM IST (10:00 UTC).

Reads:   data/stage_3/master_ensemble_signals.csv  (committed to repo)
         data/stage_3/trade_log.csv                (committed to repo, appended weekly)
Writes:  data/stage_3/trade_log.csv                (updated)
         dashboard_data.json                       (served by GitHub Pages)

BTC-USD  →  ordered via Binance (BTCUSDT market order)
All ETFs →  ordered via Upstox  (AMO delivery orders)
"""

import os, json, math, requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import date, datetime
import time

# ── CONFIG ──────────────────────────────────────────────────────────────────────
UPSTOX_TOKEN    = os.environ["UPSTOX_TOKEN"]
PORTFOLIO_VALUE = float(os.environ.get("PORTFOLIO_VALUE", "60000"))
MODE            = os.environ.get("MODE", "sandbox")
BINANCE_API     = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET  = os.environ.get("BINANCE_SECRET_KEY", "")
UPSTOX_BASE     = ("https://api-sandbox.upstox.com" if MODE == "sandbox"
                   else "https://api-hft.upstox.com")

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
    "BTC-USD"       : "BINANCE",          # routed to Binance, not Upstox
}

SIGNAL_FILE           = "data/stage_3/master_ensemble_signals.csv"
TRADE_LOG             = "data/stage_3/trade_log.csv"
DASHBOARD_FILE        = "dashboard_data.json"
MOMENTUM_LOYALTY_BUFFER = 0.02

# ── BINANCE CLIENT (optional) ─────────────────────────────────────────────────
b_client = None
if BINANCE_API and BINANCE_SECRET and MODE == "live":
    try:
        from binance.client import Client
        b_client = Client(BINANCE_API, BINANCE_SECRET)
        print("Binance client initialised ✓")
    except ImportError:
        print("[WARN] python-binance not installed. BTC orders skipped.")
    except Exception as e:
        print(f"[WARN] Binance init failed: {e}")

# ── USD/INR RATE ──────────────────────────────────────────────────────────────
def get_usd_inr():
    try:
        return float(yf.Ticker("INR=X").fast_info.last_price)
    except Exception:
        return 84.0   # safe fallback

print(f"{'='*65}")
print(f"RAAM RUNNER | {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} | {MODE.upper()}")
print(f"{'='*65}")

usd_inr = get_usd_inr()
print(f"USD/INR: {usd_inr:.2f}")

# ── 1. LOAD SIGNALS ──────────────────────────────────────────────────────────
signals_df     = pd.read_csv(SIGNAL_FILE, index_col="Date", parse_dates=True)
active_tickers = signals_df.columns.tolist()     # includes BTC-USD

# ── 2. DOWNLOAD PRICES (one-by-one with retry — GitHub Actions IPs get rate limited) ──
print("Downloading prices (individual with retry)...")
all_tickers = active_tickers + ["LIQUIDBEES.NS"]

def download_with_retry(tickers, period="180d", max_retries=5):
    frames = {}
    for ticker in tickers:
        for attempt in range(max_retries):
            try:
                time.sleep(2 + attempt * 3)
                raw = yf.download(ticker, period=period, auto_adjust=True,
                                  progress=False, timeout=30)
                if raw.empty:
                    raise ValueError("empty")
                frames[ticker] = raw["Close"]
                print(f"  ✓ {ticker}")
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"  ✗ {ticker} gave up: {e}")
                else:
                    print(f"  retry {attempt+1} {ticker}")
    return pd.DataFrame(frames)

raw_closes  = download_with_retry(all_tickers)
nifty_dates = raw_closes["NIFTYBEES.NS"].dropna().index
closes      = raw_closes.loc[raw_closes.index.isin(nifty_dates)].ffill()

# ── 3. ALIGN SIGNALS + PRICES ────────────────────────────────────────────────
aligned  = signals_df.join(closes, how="inner", lsuffix="_sig", rsuffix="_px")
signals  = aligned[[c + "_sig" for c in active_tickers]].rename(
               columns={c + "_sig": c for c in active_tickers})
prices   = aligned[closes.columns]
daily_rets = prices.pct_change().fillna(0)
mom_12w    = prices[active_tickers].pct_change(periods=60).fillna(0)

# ── 4. THIS WEEK'S SIGNAL ────────────────────────────────────────────────────
today_date = signals.index[-1]
today_sig  = signals.loc[today_date]
uptrend    = today_sig[today_sig == 1.0].index.tolist()
print(f"Signal date: {today_date.date()} | Uptrend assets ({len(uptrend)}): {uptrend}")

# ── 5. HYSTERESIS: load previous top-3 from trade log ────────────────────────
current_top3 = []
if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    recent_buys = tl[tl["action"] == "BUY"].tail(10)
    if not recent_buys.empty:
        current_top3 = recent_buys["ticker"].unique().tolist()

today_mom = mom_12w.loc[today_date, uptrend].copy() if uptrend else pd.Series(dtype=float)
for asset in current_top3:
    if asset in today_mom.index:
        today_mom[asset] += MOMENTUM_LOYALTY_BUFFER

# ── 6. TARGET ALLOCATION ─────────────────────────────────────────────────────
if not uptrend:
    target_alloc = {"LIQUIDBEES.NS": 1.0}
    new_top3     = []
else:
    new_top3 = today_mom.nlargest(3).index.tolist()
    n = len(new_top3)
    w = min(1.0 / n, 0.40)
    target_alloc = {t: w for t in new_top3}
    target_alloc["LIQUIDBEES.NS"] = max(0.0, 1.0 - sum(target_alloc.values()))

print(f"Target allocation: { {k: f'{v*100:.0f}%' for k,v in target_alloc.items()} }")

# ── 7. LIVE PRICES ────────────────────────────────────────────────────────────
def live_price_inr(ticker):
    """Returns price in INR. BTC converted from USD."""
    try:
        if ticker == "BTC-USD":
            if b_client:
                usd = float(b_client.get_symbol_ticker(symbol="BTCUSDT")["price"])
            else:
                usd = float(yf.Ticker("BTC-USD").fast_info.last_price)
            return usd * usd_inr
        p = yf.Ticker(ticker).fast_info
        return float(p.last_price or p.previous_close)
    except Exception:
        col = ticker if ticker in closes.columns else None
        return float(closes[col].iloc[-1]) if col else None

live_prices = {t: live_price_inr(t) for t in target_alloc}

# Compute target shares
target_shares = {}
for ticker, weight in target_alloc.items():
    p = live_prices.get(ticker)
    if not p or p <= 0:
        continue
    rupees = weight * PORTFOLIO_VALUE
    # BTC: fractional, 5dp. ETFs: whole shares floor.
    qty = round(rupees / p, 5) if ticker == "BTC-USD" else math.floor(rupees / p)
    if qty > 0:
        target_shares[ticker] = qty

# ── 8. PLACE ORDERS ──────────────────────────────────────────────────────────
upstox_headers = {
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}
trade_records = []

def log_trade(ticker, action, qty, price, order_id):
    trade_records.append({
        "date":     date.today().isoformat(),
        "ticker":   ticker,
        "action":   action,
        "quantity": qty,
        "price":    round(price, 2),
        "order_id": order_id,
        "mode":     MODE,
    })

for ticker, qty in target_shares.items():
    p = live_prices.get(ticker, 0)

    # ── BTC via Binance ───────────────────────────────────────────
    if ticker == "BTC-USD":
        if MODE == "sandbox":
            oid = f"SIM-BNB-{datetime.now().strftime('%H%M%S')}"
            print(f"  [SANDBOX] BUY {qty:.5f} BTC @ ₹{p:,.0f}/BTC (${p/usd_inr:,.0f})")
        elif b_client:
            try:
                from binance.client import Client
                order = b_client.order_market_buy(symbol="BTCUSDT", quantity=qty)
                oid   = str(order["orderId"])
                print(f"  [LIVE-BNB] BUY {qty} BTC → order {oid}")
            except Exception as e:
                print(f"  [ERROR] Binance BTC order failed: {e}")
                oid = "FAILED"
        else:
            print("  [SKIP] BTC-USD: no Binance credentials. Skipping.")
            continue
        log_trade(ticker, "BUY", qty, p, oid)

    # ── ETFs via Upstox ───────────────────────────────────────────
    else:
        inst = INSTRUMENT_MAP.get(ticker)
        if not inst:
            print(f"  [SKIP] {ticker}: no instrument token")
            continue
        payload = {
            "quantity": int(qty), "product": "D", "validity": "DAY",
            "price": 0, "tag": "RAAM", "instrument_token": inst,
            "order_type": "MARKET", "transaction_type": "BUY",
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": True,
        }
        if MODE == "sandbox":
            resp = requests.post(f"{UPSTOX_BASE}/v2/order/place",
                                 json=payload, headers=upstox_headers, timeout=15)
            if resp.ok:
                oid = resp.json().get("data", {}).get("order_id", "SIM-OK")
                print(f"  [SANDBOX] BUY {qty} {ticker} → {oid}")
            else:
                oid = "FAILED"
                print(f"  [ERROR] {ticker}: {resp.text[:120]}")
        else:
            resp = requests.post(f"{UPSTOX_BASE}/v2/order/place",
                                 json=payload, headers=upstox_headers, timeout=15)
            oid  = resp.json().get("data", {}).get("order_id", "FAILED") if resp.ok else "FAILED"
            print(f"  [{'OK' if resp.ok else 'FAIL'}] BUY {qty} {ticker} → {oid}")
        log_trade(ticker, "BUY", qty, p, oid)

# ── 9. UPDATE TRADE LOG ──────────────────────────────────────────────────────
os.makedirs("data/stage_3", exist_ok=True)
if trade_records:
    df_new = pd.DataFrame(trade_records)
    if os.path.exists(TRADE_LOG):
        df_all = pd.concat([pd.read_csv(TRADE_LOG), df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all.to_csv(TRADE_LOG, index=False)
    print(f"Trade log: {len(df_all)} total rows → {TRADE_LOG}")

# ── 10. COMPUTE P&L FROM TRADE LOG ──────────────────────────────────────────
holdings, cost_basis = {}, {}

if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    # Ensure numeric types
    tl["quantity"] = pd.to_numeric(tl["quantity"], errors="coerce").fillna(0)
    tl["price"]    = pd.to_numeric(tl["price"],    errors="coerce").fillna(0)

    for _, r in tl.iterrows():
        t = r["ticker"]
        qty, px = float(r["quantity"]), float(r["price"])
        holdings.setdefault(t, 0.0)
        cost_basis.setdefault(t, 0.0)
        if r["action"] == "BUY":
            holdings[t]   += qty
            cost_basis[t] += qty * px
        elif r["action"] == "SELL" and holdings[t] > 0:
            avg_cost       = cost_basis[t] / holdings[t]
            holdings[t]   -= qty
            cost_basis[t] -= qty * avg_cost

positions_list = []
total_inv = total_cur = 0.0

for t, qty in holdings.items():
    if qty < 0.0001:
        continue
    lp  = live_price_inr(t) or 0.0
    inv = cost_basis.get(t, 0.0)
    cur = qty * lp
    pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0.0
    positions_list.append({
        "ticker":     t,
        "shares":     round(qty, 5),
        "avg_cost":   round(inv / qty, 2) if qty > 0 else 0,
        "live_price": round(lp, 2),
        "invested":   round(inv, 0),
        "current":    round(cur, 0),
        "pnl":        round(pnl, 0),
        "pnl_pct":    round(pnl_pct, 2),
    })
    total_inv += inv
    total_cur += cur

net_pnl     = total_cur - total_inv
net_pnl_pct = (net_pnl / total_inv * 100) if total_inv > 0 else 0.0

# ── 11. WRITE dashboard_data.json ────────────────────────────────────────────
dashboard = {
    "generated_at":    datetime.utcnow().isoformat() + "Z",
    "mode":            MODE,
    "portfolio_value": PORTFOLIO_VALUE,
    "total_invested":  round(total_inv, 0),
    "current_value":   round(total_cur, 0),
    "net_pnl":         round(net_pnl, 0),
    "net_pnl_pct":     round(net_pnl_pct, 2),
    "breadth":         int(today_sig.sum()),
    "breadth_max":     len(active_tickers),
    "target_alloc":    {k: round(v * 100, 1) for k, v in target_alloc.items()},
    "positions":       positions_list,
    "usd_inr":         round(usd_inr, 2),
    "last_run_date":   date.today().isoformat(),
    "signal_date":     str(today_date.date()),
    "next_rebalance":  "Next Friday 3:30 PM IST",
}

with open(DASHBOARD_FILE, "w") as f:
    json.dump(dashboard, f, indent=2)

print(f"\n{'='*65}")
print(f"DONE | P&L: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%)")
print(f"Dashboard → {DASHBOARD_FILE}")
print(f"{'='*65}")
