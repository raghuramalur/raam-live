import os, json, requests
import pandas as pd
from datetime import date, datetime, timedelta
from urllib.parse import quote

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPSTOX_TOKEN     = os.environ.get("UPSTOX_TOKEN", "")
MODE             = os.environ.get("MODE", "sandbox")
UPSTOX_HIST_BASE = "https://api.upstox.com"

TRADE_LOG        = "data/stage_3/trade_log.csv"
DASHBOARD_FILE   = "dashboard_data.json"

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

print(f"Market: {MODE.upper()}")

# ── HELPER: strip timezone ────────────────────────────────────────────────────
def strip_tz(ts):
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.normalize()

# ── UPSTOX HISTORICAL DAILY CLOSES ────────────────────────────────────────────
def get_upstox_daily_closes(instrument_key, days_back=10):
    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    encoded   = quote(instrument_key, safe="")
    url = f"{UPSTOX_HIST_BASE}/v3/historical-candle/{encoded}/days/1/{to_date}/{from_date}"
    try:
        r = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        if r.status_code == 200:
            candles = r.json()["data"]["candles"]
            rows = {}
            for c in candles:
                dt = strip_tz(c[0])
                rows[dt] = float(c[4])
            return pd.Series(rows).sort_index()
    except Exception as e:
        print(f"  [WARN] {instrument_key} historical fetch error: {e}")
    return pd.Series(dtype=float)

# ── LIVE PRICE IN INR (EXACT COPY FROM RUNNER) ────────────────────────────────
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
    if not inst: return None
        
    try:
        encoded = quote(inst, safe="")
        url = f"{UPSTOX_HIST_BASE}/v2/market-quote/quotes?instrument_key={encoded}"
        r   = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        if r.status_code == 200:
            for v in r.json().get("data", {}).values():
                ltp = v.get("last_price") or v.get("ltp") or 0
                if ltp > 0: return float(ltp)
    except Exception as e:
        print(f"  [WARN] live price {ticker}: {e}")

    # Fallback: last close from historical (Bypasses V2 Blocks)
    print(f"  [DEBUG] Falling back to historical for {ticker}")
    s = get_upstox_daily_closes(inst, days_back=5)
    return float(s.iloc[-1]) if len(s) > 0 else None

# ── P&L CALCULATION ──────────────────────────────────────────────────────────
if not os.path.exists(TRADE_LOG):
    print("No trade log found. P&L: ₹0")
    exit()

holdings, cost_basis = {}, {}
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
    
    # Ultimate failsafe: If price is exactly 0 from APIs, use buy price
    if lp == 0.0 and qty > 0:
        lp = inv / qty
        
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

# ── UPDATE DASHBOARD JSON ────────────────────────────────────────────────────
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

print(f"P&L: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%) | Invested: ₹{total_inv:,.0f} | Current: ₹{total_cur:,.0f}")
print(f"Updated @ {datetime.utcnow().strftime('%H:%M')} UTC")
