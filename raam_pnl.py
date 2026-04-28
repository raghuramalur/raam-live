"""
raam_pnl.py — Live P&L updater, runs every 5 min during market hours.
When market is closed, falls back to last known close price from Upstox
historical API so P&L shows correct values 24/7.
"""
import os, json, requests
import pandas as pd
from datetime import datetime, date, timedelta
from urllib.parse import quote

UPSTOX_TOKEN    = os.environ.get("UPSTOX_TOKEN", "")
PORTFOLIO_VALUE = float(os.environ.get("PORTFOLIO_VALUE", "60000"))
MODE            = os.environ.get("MODE", "sandbox")
TRADE_LOG       = "data/stage_3/trade_log.csv"
DASHBOARD_FILE  = "dashboard_data.json"
UPSTOX_BASE     = "https://api.upstox.com"

AUTH_HEADERS = {
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

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
    "BTC-USD"       : None,
}


def get_upstox_last_close(instrument_key):
    """Fetch the most recent daily close from Upstox historical API."""
    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    encoded   = quote(instrument_key, safe="")
    url = (f"{UPSTOX_BASE}/v3/historical-candle"
           f"/{encoded}/days/1/{to_date}/{from_date}")
    try:
        r = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        if r.ok:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                val = float(candles[0][4])
                print(f"    [DEBUG] Upstox V3 Historical returned: ₹{val}")
                return val
        else:
            print(f"    [DEBUG] Upstox V3 Error {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"    [DEBUG] Upstox V3 Exception: {e}")
    return None


def live_price_inr(ticker):
    print(f"\n  ➤ Fetching pricing data for: {ticker}")
    if ticker == "BTC-USD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=inr",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            val = float(r.json()["bitcoin"]["inr"])
            print(f"    [DEBUG] CoinGecko returned: ₹{val}")
            return val
        except Exception as e:
            print(f"    [DEBUG] CoinGecko Exception: {e}")
            return None

    inst = INSTRUMENT_MAP.get(ticker)
    if not inst:
        return None

    ltp = None
    try:
        enc = quote(inst, safe="")
        url = f"{UPSTOX_BASE}/v2/market-quote/ltp?instrument_key={enc}"
        r = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        
        print(f"    [DEBUG] Upstox V2 LTP HTTP Status: {r.status_code}")
        if r.ok:
            data = r.json().get("data", {})
            if inst in data:
                p = data[inst].get("last_price")
                print(f"    [DEBUG] Upstox V2 LTP exact value: {p}")
                if p and float(p) > 0:
                    ltp = float(p)
            else:
                print(f"    [DEBUG] Instrument key not found in LTP response.")
        else:
             print(f"    [DEBUG] Upstox V2 LTP Response: {r.text[:80]}")
    except Exception as e:
        print(f"    [DEBUG] Upstox V2 LTP Exception: {e}")

    # Fallback Trigger
    if not ltp or ltp == 0:
        print(f"    [DEBUG] Live quote failed or returned 0. Falling back to Historical...")
        ltp = get_upstox_last_close(inst)

    print(f"    [DEBUG] Final Resolved Price for {ticker}: ₹{ltp}")
    return ltp


# ── Load existing dashboard ──────────────────────────────────────────────────
existing = {}
if os.path.exists(DASHBOARD_FILE):
    try:
        with open(DASHBOARD_FILE) as f:
            existing = json.load(f)
    except Exception:
        pass

# ── Compute holdings from trade log ──────────────────────────────────────────
holdings, cost_basis = {}, {}
if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    tl["quantity"] = pd.to_numeric(tl["quantity"], errors="coerce").fillna(0)
    tl["price"]    = pd.to_numeric(tl["price"],    errors="coerce").fillna(0)

    if "order_id" in tl.columns:
        tl = tl.drop_duplicates(subset=["order_id"], keep="first")

    for _, r in tl.iterrows():
        t = r["ticker"]; qty = float(r["quantity"]); px = float(r["price"])
        holdings.setdefault(t, 0.0); cost_basis.setdefault(t, 0.0)
        if r["action"] == "BUY":
            holdings[t] += qty;  cost_basis[t] += qty * px
        elif r["action"] == "SELL" and holdings[t] > 0:
            avg = cost_basis[t] / holdings[t]
            holdings[t] -= qty;  cost_basis[t] -= qty * avg

# ── Fetch live prices + compute P&L ──────────────────────────────────────────
positions_list = []
total_inv = total_cur = 0.0

for t, qty in holdings.items():
    if qty < 0.0001:
        continue
        
    lp  = live_price_inr(t)
    
    if lp is None or lp == 0:
        print(f"  [WARN] Final price is 0 for {t}, skipping P&L calculation for this asset.")
        continue
        
    inv = cost_basis.get(t, 0.0)
    avg_cost = inv / qty if qty > 0 else 0
    cur = qty * lp
    pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0.0
    
    print(f"  [RESULT] {t} | Avg Cost: ₹{avg_cost:.2f} | Live: ₹{lp:.2f} | P&L: ₹{pnl:.2f}")
    
    positions_list.append({
        "ticker"    : t,
        "shares"    : round(qty, 5),
        "avg_cost"  : round(avg_cost, 2),
        "live_price": round(lp, 2),
        "invested"  : round(inv, 0),
        "current"   : round(cur, 0),
        "pnl"       : round(pnl, 0),
        "pnl_pct"   : round(pnl_pct, 2),
    })
    total_inv += inv
    total_cur += cur

net_pnl     = total_cur - total_inv
net_pnl_pct = (net_pnl / total_inv * 100) if total_inv > 0 else 0.0

# ── Check if market is open (IST 9:15-15:30 on weekdays) ─────────────────────
now_ist = datetime.utcnow().replace(tzinfo=None)
ist_hour   = (now_ist.hour + 5) % 24
ist_minute = (now_ist.minute + 30) % 60
if now_ist.minute >= 30:
    ist_hour = (now_ist.hour + 5 + 1) % 24
ist_time = ist_hour * 60 + ist_minute   
market_open = (9 * 60 + 15) <= ist_time <= (15 * 60 + 30)
market_status = "LIVE" if market_open else "CLOSED — showing last close"

# ── Merge with existing dashboard data ───────────────────────────────────────
dashboard = {
    **existing,
    "generated_at"    : datetime.utcnow().isoformat() + "Z",
    "mode"            : MODE,
    "portfolio_value" : PORTFOLIO_VALUE,
    "total_invested"  : round(total_inv, 0),
    "current_value"   : round(total_cur, 0),
    "net_pnl"         : round(net_pnl, 0),
    "net_pnl_pct"     : round(net_pnl_pct, 2),
    "positions"       : positions_list,
    "market_status"   : market_status,
    "last_pnl_update" : datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
}

with open(DASHBOARD_FILE, "w") as f:
    json.dump(dashboard, f, indent=2)

print(f"\n==============================================")
print(f"Market Status: {market_status}")
print(f"TOTAL P&L: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%)")
print(f"Invested: ₹{total_inv:,.0f} | Current: ₹{total_cur:,.0f}")
print(f"Updated @ {datetime.utcnow().strftime('%H:%M UTC')}")
print(f"==============================================")
