"""
raam_pnl.py — Live P&L updater, runs every 5 min during market hours.
When market is closed, falls back to last known close price from Upstox
historical API so P&L shows correct values 24/7.
"""
import os, json, requests
import pandas as pd
from datetime import datetime, date, timedelta
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
                return float(candles[0][4])  # most recent close
    except Exception:
        pass
    return None


def live_price_inr(ticker):
    """
    Try live market quote first.
    If market is closed (returns 0), fall back to last historical close.
    """
    if ticker == "BTC-USD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=inr",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            return float(r.json()["bitcoin"]["inr"])
        except Exception:
            return None

    inst = INSTRUMENT_MAP.get(ticker)
    if not inst:
        return None

    # Try live quote
    ltp = None
    try:
        enc = quote(inst, safe="")
        r = requests.get(
            f"{UPSTOX_BASE}/v2/market-quote/quotes?instrument_key={enc}",
            headers=AUTH_HEADERS, timeout=10
        )
        if r.ok:
            for v in r.json().get("data", {}).values():
                p = v.get("last_price") or v.get("ltp") or 0
                if p and float(p) > 0:
                    ltp = float(p)
                    break
    except Exception:
        pass

    # If live quote returned 0 or failed → market closed → use last close
    if not ltp or ltp == 0:
        ltp = get_upstox_last_close(inst)

    return ltp


# ── Load existing dashboard (keep weekly strategy fields) ─────────────────────
existing = {}
if os.path.exists(DASHBOARD_FILE):
    with open(DASHBOARD_FILE) as f:
        existing = json.load(f)

# ── Compute holdings from trade log ──────────────────────────────────────────
holdings, cost_basis = {}, {}
if os.path.exists(TRADE_LOG):
    tl = pd.read_csv(TRADE_LOG)
    tl["quantity"] = pd.to_numeric(tl["quantity"], errors="coerce").fillna(0)
    tl["price"]    = pd.to_numeric(tl["price"],    errors="coerce").fillna(0)

    # Deduplicate: if same order_id appears more than once, keep only first
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
market_closed = False

for t, qty in holdings.items():
    if qty < 0.0001:
        continue
    lp  = live_price_inr(t)
    if lp is None or lp == 0:
        print(f"  [WARN] No price for {t}, skipping")
        continue
    inv = cost_basis.get(t, 0.0)
    cur = qty * lp
    pnl = cur - inv
    pnl_pct = (pnl / inv * 100) if inv > 0 else 0.0
    positions_list.append({
        "ticker"    : t,
        "shares"    : round(qty, 5),
        "avg_cost"  : round(inv / qty, 2) if qty else 0,
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
# UTC+5:30
ist_hour   = (now_ist.hour + 5) % 24
ist_minute = (now_ist.minute + 30) % 60
if now_ist.minute >= 30:
    ist_hour = (now_ist.hour + 5 + 1) % 24
ist_time = ist_hour * 60 + ist_minute   # minutes since midnight IST
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

print(f"Market: {market_status}")
print(f"P&L: ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%) | "
      f"Invested: ₹{total_inv:,.0f} | Current: ₹{total_cur:,.0f}")
print(f"Updated @ {datetime.utcnow().strftime('%H:%M UTC')}")
