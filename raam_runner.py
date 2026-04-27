"""
raam_runner.py  —  RAAM Weekly Execution Engine v4
Data sources:
  - Indian ETFs : Upstox Historical Candle Data V3 API
  - BTC closes  : CoinGecko public API (free, no auth, not blocked by GH Actions)
  - USD/INR rate: exchangerate-api.com free endpoint
  - BTC orders  : Binance authenticated API (if keys provided)
  - ETF orders  : Upstox order placement API
"""

import os, json, math, time, requests
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from urllib.parse import quote

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPSTOX_TOKEN    = os.environ["UPSTOX_TOKEN"]
PORTFOLIO_VALUE = float(os.environ.get("PORTFOLIO_VALUE", "60000"))
MODE            = os.environ.get("MODE", "sandbox")
BINANCE_API     = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET  = os.environ.get("BINANCE_SECRET_KEY", "")

UPSTOX_HIST_BASE  = "https://api.upstox.com"
UPSTOX_ORDER_BASE = ("https://api-sandbox.upstox.com" if MODE == "sandbox"
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
    "BTC-USD"       : "BINANCE",
}

SIGNAL_FILE             = "data/stage_3/master_ensemble_signals.csv"
TRADE_LOG               = "data/stage_3/trade_log.csv"
DASHBOARD_FILE          = "dashboard_data.json"
MOMENTUM_LOYALTY_BUFFER = 0.02
HIST_DAYS               = 100

AUTH_HEADERS = {
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
    "Content-Type" : "application/json",
    "Accept"       : "application/json",
}

print(f"{'='*65}")
print(f"RAAM RUNNER v4 | {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | {MODE.upper()}")
print(f"Data: Upstox Historical API + CoinGecko BTC")
print(f"{'='*65}")

# ── 1. USD/INR RATE ───────────────────────────────────────────────────────────
def get_usd_inr():
    for url in ["https://api.exchangerate-api.com/v4/latest/USD",
                "https://open.er-api.com/v6/latest/USD"]:
        try:
            r = requests.get(url, timeout=10)
            return float(r.json()["rates"]["INR"])
        except Exception:
            continue
    print("[WARN] USD/INR fetch failed, using 84.0")
    return 84.0

usd_inr = get_usd_inr()
print(f"USD/INR: {usd_inr:.2f}")

# ── 2. UPSTOX HISTORICAL DAILY CLOSES ────────────────────────────────────────
def get_upstox_daily_closes(instrument_key, days_back=HIST_DAYS):
    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=days_back + 30)).strftime("%Y-%m-%d")
    encoded   = quote(instrument_key, safe="")
    url = (f"{UPSTOX_HIST_BASE}/v3/historical-candle"
           f"/{encoded}/days/1/{to_date}/{from_date}")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=AUTH_HEADERS, timeout=15)
            if r.status_code == 200:
                candles = r.json()["data"]["candles"]
                rows = {}
                for c in candles:
                    # candle = [timestamp, open, high, low, close, volume, oi]
                    dt = pd.Timestamp(c[0]).normalize()  # ← Timestamp, not date
                    rows[dt] = float(c[4])
                return pd.Series(rows).sort_index()
            else:
                print(f"  [WARN] {instrument_key} HTTP {r.status_code}: {r.text[:80]}")
        except Exception as e:
            print(f"  [WARN] {instrument_key} attempt {attempt+1}: {e}")
        time.sleep(1)
    return pd.Series(dtype=float)

# ── 3. BTC HISTORICAL CLOSES IN INR (COINGECKO) ──────────────────────────────
def get_btc_daily_closes_inr(days_back=HIST_DAYS):
    """
    CoinGecko public API — free, no auth, works from GitHub Actions.
    Returns INR prices directly so no USD/INR conversion needed.
    """
    url = (f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
           f"?vs_currency=inr&days={days_back}&interval=daily")
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        prices = r.json()["prices"]  # [[timestamp_ms, price_inr], ...]
        rows = {}
        for p in prices:
            # Normalize to midnight Timestamp to match other tickers
            dt = pd.Timestamp(p[0], unit="ms").normalize()
            rows[dt] = float(p[1])
        s = pd.Series(rows).sort_index()
        print(f"  ✓ BTC-USD ({len(s)} days, CoinGecko INR)")
        return s
    except Exception as e:
        print(f"  ✗ BTC-USD CoinGecko failed: {e}")
        return pd.Series(dtype=float)

# ── 4. LIVE PRICE IN INR ─────────────────────────────────────────────────────
def get_live_price_inr(ticker):
    if ticker == "BTC-USD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=inr",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            return float(r.json()["bitcoin"]["inr"])
        except Exception as e:
            print(f"  [WARN] BTC live price: {e}")
            return None

    inst = INSTRUMENT_MAP.get(ticker)
    if not inst:
        return None
    try:
        encoded = quote(inst, safe="")
        url = f"{UPSTOX_HIST_BASE}/v2/market-quote/quotes?instrument_key={encoded}"
        r   = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        if r.status_code == 200:
            for v in r.json().get("data", {}).values():
                ltp = v.get("last_price") or v.get("ltp") or 0
                if ltp > 0:
                    return float(ltp)
    except Exception as e:
        print(f"  [WARN] live price {ticker}: {e}")

    # Fallback: last close from historical
    s = get_upstox_daily_closes(inst, days_back=5)
    return float(s.iloc[-1]) if len(s) > 0 else None

# ── 5. BUILD CLOSES DATAFRAME ─────────────────────────────────────────────────
print("\nDownloading historical closes...")
signals_df     = pd.read_csv(SIGNAL_FILE, index_col="Date", parse_dates=True)
active_tickers = signals_df.columns.tolist()

closes_dict = {}
for ticker in active_tickers + ["LIQUIDBEES.NS"]:
    if ticker == "BTC-USD":
        s = get_btc_daily_closes_inr()
        if len(s) > 0:
            closes_dict[ticker] = s
        else:
            print(f"  ✗ BTC-USD: no data")
        continue

    inst = INSTRUMENT_MAP.get(ticker)
    if not inst:
        print(f"  ✗ {ticker}: no instrument key")
        continue

    s = get_upstox_daily_closes(inst)
    if len(s) > 0:
        closes_dict[ticker] = s
        print(f"  ✓ {ticker} ({len(s)} days)")
    else:
        print(f"  ✗ {ticker}: no data")
    time.sleep(0.3)

if len(closes_dict) < 5:
    raise RuntimeError(f"Only {len(closes_dict)} tickers loaded. Aborting.")

# Build DataFrame with DatetimeIndex — everything normalized to midnight Timestamps
raw_closes  = pd.DataFrame(closes_dict)
raw_closes.index = pd.to_datetime(raw_closes.index)   # ← FIX: ensure DatetimeIndex
nifty_dates = raw_closes["NIFTYBEES.NS"].dropna().index
closes      = raw_closes.loc[raw_closes.index.isin(nifty_dates)].ffill()

print(f"\nCloses matrix: {closes.shape[0]} rows × {closes.shape[1]} columns")

# ── 6. ALIGN SIGNALS + COMPUTE MOMENTUM ──────────────────────────────────────
# Both signals_df and closes now have Timestamp DatetimeIndex — safe to join
aligned  = signals_df.join(closes, how="inner", lsuffix="_sig", rsuffix="_px")
sigs_cols = [c for c in aligned.columns if c.endswith("_sig")]
signals  = aligned[sigs_cols].rename(columns=lambda c: c.replace("_sig", ""))

available_active = [t for t in active_tickers if t in closes.columns]
mom_12w = closes[available_active].pct_change(periods=60).fillna(0)

# ── 7. THIS WEEK'S SIGNAL + HYSTERESIS ───────────────────────────────────────
today_date = signals.index[-1]
today_sig  = signals.loc[today_date]
uptrend    = today_sig[today_sig == 1.0].index.tolist()
print(f"\nSignal date: {today_date.date()} | Uptrend ({len(uptrend)}): {uptrend}")

current_top3 = []
if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    recent_buys = tl[tl["action"] == "BUY"].tail(10)
    if not recent_buys.empty:
        current_top3 = recent_buys["ticker"].unique().tolist()

valid_uptrend = [t for t in uptrend if t in mom_12w.columns]
if valid_uptrend and today_date in mom_12w.index:
    today_mom = mom_12w.loc[today_date, valid_uptrend].copy()
    for asset in current_top3:
        if asset in today_mom.index:
            today_mom[asset] += MOMENTUM_LOYALTY_BUFFER
else:
    today_mom = pd.Series(dtype=float)

# ── 8. TARGET ALLOCATION ─────────────────────────────────────────────────────
if today_mom.empty:
    target_alloc = {"LIQUIDBEES.NS": 1.0}
else:
    new_top3 = today_mom.nlargest(3).index.tolist()
    n = len(new_top3)
    w = min(1.0 / n, 0.40)
    target_alloc = {t: w for t in new_top3}
    target_alloc["LIQUIDBEES.NS"] = max(0.0, 1.0 - sum(target_alloc.values()))

print(f"Target: { {k: f'{v*100:.0f}%' for k,v in target_alloc.items()} }")

# ── 9. LIVE PRICES + SHARE QUANTITIES ────────────────────────────────────────
live_prices   = {t: get_live_price_inr(t) for t in target_alloc}
target_shares = {}

for ticker, weight in target_alloc.items():
    p = live_prices.get(ticker)
    if not p or p <= 0:
        print(f"  [SKIP] {ticker}: no live price")
        continue
    rupees = weight * PORTFOLIO_VALUE
    qty    = round(rupees / p, 5) if ticker == "BTC-USD" else math.floor(rupees / p)
    if qty > 0:
        target_shares[ticker] = qty
        print(f"  {ticker}: {weight*100:.0f}% = ₹{rupees:,.0f} @ ₹{p:,.2f} = {qty}")

# ── 10. PLACE ORDERS ─────────────────────────────────────────────────────────
b_client = None
if BINANCE_API and BINANCE_SECRET and MODE == "live":
    try:
        from binance.client import Client
        b_client = Client(BINANCE_API, BINANCE_SECRET)
        print("Binance client ready ✓")
    except Exception as e:
        print(f"[WARN] Binance init: {e}")

trade_records = []

def log_trade(ticker, action, qty, price, order_id):
    trade_records.append({
        "date"    : date.today().isoformat(),
        "ticker"  : ticker,
        "action"  : action,
        "quantity": qty,
        "price"   : round(price, 2),
        "order_id": order_id,
        "mode"    : MODE,
    })

print("\nPlacing orders...")
for ticker, qty in target_shares.items():
    p = live_prices.get(ticker, 0)

    if ticker == "BTC-USD":
        if MODE == "sandbox":
            oid = f"SIM-BNB-{datetime.utcnow().strftime('%H%M%S')}"
            print(f"  [SANDBOX] BUY {qty:.5f} BTC @ ₹{p:,.0f}")
        elif b_client:
            try:
                order = b_client.order_market_buy(symbol="BTCUSDT", quantity=qty)
                oid   = str(order["orderId"])
                print(f"  [LIVE] BUY {qty} BTC → {oid}")
            except Exception as e:
                print(f"  [ERROR] Binance: {e}"); oid = "FAILED"
        else:
            print("  [SKIP] BTC: no Binance credentials"); continue
        log_trade(ticker, "BUY", qty, p, oid)

    else:
        inst = INSTRUMENT_MAP.get(ticker)
        if not inst:
            continue
        payload = {
            "quantity": int(qty), "product": "D", "validity": "DAY",
            "price": 0, "tag": "RAAM", "instrument_token": inst,
            "order_type": "MARKET", "transaction_type": "BUY",
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": True,
        }
        resp = requests.post(
            f"{UPSTOX_ORDER_BASE}/v2/order/place",
            json=payload, headers=AUTH_HEADERS, timeout=15
        )
        if resp.ok:
            oid = resp.json().get("data", {}).get("order_id", "OK")
            print(f"  [{'SANDBOX' if MODE=='sandbox' else 'LIVE'}] BUY {qty} {ticker} → {oid}")
        else:
            oid = "FAILED"
            print(f"  [ERROR] {ticker}: {resp.text[:120]}")
        log_trade(ticker, "BUY", qty, p, oid)

# ── 11. UPDATE TRADE LOG ─────────────────────────────────────────────────────
os.makedirs("data/stage_3", exist_ok=True)
if trade_records:
    df_new = pd.DataFrame(trade_records)
    if os.path.exists(TRADE_LOG):
        df_all = pd.concat([pd.read_csv(TRADE_LOG), df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all.to_csv(TRADE_LOG, index=False)
    print(f"Trade log: {len(df_all)} total rows")

# ── 12. P&L FROM TRADE LOG ───────────────────────────────────────────────────
holdings, cost_basis = {}, {}
if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    tl["quantity"] = pd.to_numeric(tl["quantity"], errors="coerce").fillna(0)
    tl["price"]    = pd.to_numeric(tl["price"],    errors="coerce").fillna(0)
    for _, r in tl.iterrows():
        t = r["ticker"]; qty = float(r["quantity"]); px = float(r["price"])
        holdings.setdefault(t, 0.0); cost_basis.setdefault(t, 0.0)
        if r["action"] == "BUY":
            holdings[t] += qty;  cost_basis[t] += qty * px
        elif r["action"] == "SELL" and holdings[t] > 0:
            avg = cost_basis[t] / holdings[t]
            holdings[t] -= qty;  cost_basis[t] -= qty * avg

positions_list = []
total_inv = total_cur = 0.0
for t, qty in holdings.items():
    if qty < 0.0001: continue
    lp  = get_live_price_inr(t) or 0.0
    inv = cost_basis.get(t, 0.0)
    cur = qty * lp; pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0.0
    positions_list.append({
        "ticker": t, "shares": round(qty, 5),
        "avg_cost": round(inv/qty, 2) if qty else 0,
        "live_price": round(lp, 2), "invested": round(inv, 0),
        "current": round(cur, 0), "pnl": round(pnl, 0),
        "pnl_pct": round(pnl_pct, 2),
    })
    total_inv += inv; total_cur += cur

net_pnl     = total_cur - total_inv
net_pnl_pct = (net_pnl / total_inv * 100) if total_inv > 0 else 0.0

# ── 13. WRITE DASHBOARD JSON ─────────────────────────────────────────────────
dashboard = {
    "generated_at"   : datetime.utcnow().isoformat() + "Z",
    "mode"           : MODE,
    "portfolio_value": PORTFOLIO_VALUE,
    "total_invested" : round(total_inv, 0),
    "current_value"  : round(total_cur, 0),
    "net_pnl"        : round(net_pnl, 0),
    "net_pnl_pct"    : round(net_pnl_pct, 2),
    "breadth"        : int(today_sig.sum()),
    "breadth_max"    : len(active_tickers),
    "target_alloc"   : {k: round(v*100, 1) for k, v in target_alloc.items()},
    "positions"      : positions_list,
    "usd_inr"        : round(usd_inr, 2),
    "last_run_date"  : date.today().isoformat(),
    "signal_date"    : str(today_date.date()),
}

with open(DASHBOARD_FILE, "w") as f:
    json.dump(dashboard, f, indent=2)

print(f"\n{'='*65}")
print(f"DONE | P&L ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%) | → {DASHBOARD_FILE}")
print(f"{'='*65}")
