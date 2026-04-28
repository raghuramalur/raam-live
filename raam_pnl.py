import os, json, requests
import pandas as pd
from datetime import datetime, timedelta, date
from urllib.parse import quote
import yfinance as yf

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPSTOX_TOKEN   = os.environ.get("UPSTOX_TOKEN", "")
MODE           = os.environ.get("MODE", "sandbox")
UPSTOX_HIST_BASE = "https://api.upstox.com"

TRADE_LOG      = "data/stage_3/trade_log.csv"
DASHBOARD_FILE = "dashboard_data.json"

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

AUTH_HEADERS = {
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
    "Content-Type" : "application/json",
    "Accept"       : "application/json",
}

# ── LIVE PRICE FETCHING (TRIPLE FALLBACK) ─────────────────────────────────────
def get_live_price_inr(ticker):
    """Fetches live prices with fail-safes and strict error logging."""
    # 1. Handle Bitcoin (CoinGecko)
    if ticker == "BTC-USD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=inr",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            return float(r.json()["bitcoin"]["inr"])
        except Exception as e:
            print(f"  [WARN] BTC CoinGecko error: {e}")
            return None

    # 2. Handle Upstox ETFs
    inst = INSTRUMENT_MAP.get(ticker)
    if not inst: return None
    encoded = quote(inst, safe="")

    # ATTEMPT 1: Upstox V2 Quotes
    try:
        url = f"{UPSTOX_HIST_BASE}/v2/market-quote/quotes?instrument_key={encoded}"
        r   = requests.get(url, headers=AUTH_HEADERS, timeout=5)
        if r.status_code == 200:
            for v in r.json().get("data", {}).values():
                ltp = v.get("last_price") or v.get("ltp") or 0
                if ltp > 0: return float(ltp)
        else:
            print(f"  [DEBUG] Upstox V2 Quotes rejected {ticker}: HTTP {r.status_code} - {r.text[:60]}")
    except Exception as e:
        print(f"  [WARN] Upstox V2 Quotes connection error: {e}")

    # ATTEMPT 2: Yahoo Finance fast_info (Bypasses Upstox token/IP limits)
    try:
        lp = float(yf.Ticker(ticker).fast_info.last_price)
        if lp > 0: 
            print(f"  [DEBUG] Recovered {ticker} price via yfinance fast_info")
            return lp
    except Exception as e:
        print(f"  [DEBUG] Yfinance fast_info rejected {ticker}: {e}")

    # ATTEMPT 3: Upstox V3 Historical (Matches your raam_runner.py fallback)
    try:
        to_date   = date.today().strftime("%Y-%m-%d")
        from_date = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        url = f"{UPSTOX_HIST_BASE}/v3/historical-candle/{encoded}/days/1/{to_date}/{from_date}"
        r = requests.get(url, headers=AUTH_HEADERS, timeout=5)
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                print(f"  [DEBUG] Recovered {ticker} price via Upstox V3 Historical")
                return float(candles[0][4]) # Latest close price
        else:
            print(f"  [DEBUG] Upstox V3 Historical rejected: HTTP {r.status_code}")
    except Exception as e:
        print(f"  [WARN] Upstox V3 error: {e}")

    print(f"  [ERROR] CRITICAL: All 3 pricing pipelines failed for {ticker}!")
    return None

# ── CALCULATE P&L AND UPDATE DASHBOARD ────────────────────────────────────────
print(f"Market: {MODE.upper()}")

if not os.path.exists(TRADE_LOG):
    print("No trade log found. P&L: ₹0")
    exit()

# 1. Calculate Holdings & Cost Basis
tl = pd.read_csv(TRADE_LOG)
tl["quantity"] = pd.to_numeric(tl["quantity"], errors="coerce").fillna(0)
tl["price"]    = pd.to_numeric(tl["price"],    errors="coerce").fillna(0)

holdings = {}
cost_basis = {}

for _, r in tl.iterrows():
    t = r["ticker"]
    qty = float(r["quantity"])
    px = float(r["price"])
    
    holdings.setdefault(t, 0.0)
    cost_basis.setdefault(t, 0.0)
    
    if r["action"] == "BUY":
        holdings[t] += qty
        cost_basis[t] += (qty * px)
    elif r["action"] == "SELL" and holdings[t] > 0:
        avg = cost_basis[t] / holdings[t]
        holdings[t] -= qty
        cost_basis[t] -= (qty * avg)

# 2. Fetch Live Prices & Calculate P&L
positions_list = []
total_inv = 0.0
total_cur = 0.0

for t, qty in holdings.items():
    if qty < 0.0001: 
        continue 
        
    lp  = get_live_price_inr(t)
    inv = cost_basis.get(t, 0.0)
    
    # Fallback to average cost ONLY if all 3 APIs fail
    if not lp:
        lp = inv / qty if qty > 0 else 0
        
    cur = qty * lp
    pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0.0
    
    positions_list.append({
        "ticker": t, 
        "shares": round(qty, 5) if ticker == "BTC-USD" else int(qty),
        "avg_cost": round(inv/qty, 2) if qty else 0,
        "live_price": round(lp, 2), 
        "invested": round(inv, 0),
        "current": round(cur, 0), 
        "pnl": round(pnl, 0),
        "pnl_pct": round(pnl_pct, 2),
    })
    total_inv += inv
    total_cur += cur

net_pnl     = total_cur - total_inv
net_pnl_pct = (net_pnl / total_inv * 100) if total_inv > 0 else 0.0

# 3. Update Existing Dashboard JSON
dashboard = {}
if os.path.exists(DASHBOARD_FILE):
    try:
        with open(DASHBOARD_FILE, "r") as f:
            dashboard = json.load(f)
    except Exception:
        pass

dashboard["generated_at"]   = datetime.utcnow().isoformat() + "Z"
dashboard["total_invested"] = round(total_inv, 0)
dashboard["current_value"]  = round(total_cur, 0)
dashboard["net_pnl"]        = round(net_pnl, 0)
dashboard["net_pnl_pct"]    = round(net_pnl_pct, 2)
dashboard["positions"]      = positions_list

with open(DASHBOARD_FILE, "w") as f:
    json.dump(dashboard, f, indent=2)

# 4. Print Log for GitHub Actions
print(f"P&L: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%) | Invested: ₹{total_inv:,.0f} | Current: ₹{total_cur:,.0f}")
print(f"Updated @ {datetime.utcnow().strftime('%H:%M')} UTC")
