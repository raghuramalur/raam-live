"""
raam_pnl.py — Lightweight P&L updater, runs every 5 minutes.
Reads trade_log.csv, fetches live prices, writes dashboard_data.json.
No historical data download, no orders, no momentum calc.
"""
import os, json, requests
import pandas as pd
from datetime import datetime, date
from urllib.parse import quote

UPSTOX_TOKEN    = os.environ["UPSTOX_TOKEN"]
PORTFOLIO_VALUE = float(os.environ.get("PORTFOLIO_VALUE", "60000"))
MODE            = os.environ.get("MODE", "sandbox")
TRADE_LOG       = "data/stage_3/trade_log.csv"
DASHBOARD_FILE  = "dashboard_data.json"
UPSTOX_BASE     = "https://api.upstox.com"

AUTH_HEADERS = {
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
    "Accept": "application/json",
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

def live_price_inr(ticker):
    if ticker == "BTC-USD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=inr",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            return float(r.json()["bitcoin"]["inr"])
        except:
            return None
    inst = INSTRUMENT_MAP.get(ticker)
    if not inst:
        return None
    try:
        enc = quote(inst, safe="")
        r = requests.get(
            f"{UPSTOX_BASE}/v2/market-quote/quotes?instrument_key={enc}",
            headers=AUTH_HEADERS, timeout=10
        )
        if r.ok:
            for v in r.json().get("data", {}).values():
                ltp = v.get("last_price") or v.get("ltp") or 0
                if ltp > 0:
                    return float(ltp)
    except:
        pass
    return None

# Load existing dashboard to preserve allocation/signal data from weekly run
existing = {}
if os.path.exists(DASHBOARD_FILE):
    with open(DASHBOARD_FILE) as f:
        existing = json.load(f)

# Compute holdings from trade log
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
    lp  = live_price_inr(t) or 0.0
    inv = cost_basis.get(t, 0.0)
    cur = qty * lp; pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0.0
    positions_list.append({
        "ticker": t, "shares": round(qty, 5),
        "avg_cost": round(inv/qty, 2) if qty else 0,
        "live_price": round(lp, 2),
        "invested": round(inv, 0),
        "current": round(cur, 0),
        "pnl": round(pnl, 0),
        "pnl_pct": round(pnl_pct, 2),
    })
    total_inv += inv; total_cur += cur

net_pnl     = total_cur - total_inv
net_pnl_pct = (net_pnl / total_inv * 100) if total_inv > 0 else 0.0

# Merge: update P&L fields, keep allocation/signal fields from weekly run
dashboard = {
    **existing,   # keeps target_alloc, breadth, signal_date from last weekly run
    "generated_at"  : datetime.utcnow().isoformat() + "Z",
    "mode"          : MODE,
    "portfolio_value": PORTFOLIO_VALUE,
    "total_invested" : round(total_inv, 0),
    "current_value"  : round(total_cur, 0),
    "net_pnl"        : round(net_pnl, 0),
    "net_pnl_pct"    : round(net_pnl_pct, 2),
    "positions"      : positions_list,
    "last_pnl_update": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
}

with open(DASHBOARD_FILE, "w") as f:
    json.dump(dashboard, f, indent=2)

print(f"P&L updated: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%) @ {datetime.utcnow().strftime('%H:%M UTC')}")
