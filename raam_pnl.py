"""
raam_pnl.py — Live P&L updater, runs every 5 min during market hours.

Price source priority:
  1. Upstox V2 LTP API     (only works with LIVE tokens, not sandbox)
  2. NSE public quote API  (free, no auth, works for any ETF)
  3. Upstox V3 historical  (works with sandbox tokens, returns last close)

For BTC: CoinGecko returns price directly in INR — no conversion needed.
"""
import os, json, requests, time
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

# ETF ticker → (Upstox instrument key, NSE trading symbol)
INSTRUMENT_MAP = {
    "NIFTYBEES.NS"  : ("NSE_EQ|INF204KB14I2", "NIFTYBEES"),
    "JUNIORBEES.NS" : ("NSE_EQ|INF732E01045", "JUNIORBEES"),
    "GOLDBEES.NS"   : ("NSE_EQ|INF204KB17I5", "GOLDBEES"),
    "BANKBEES.NS"   : ("NSE_EQ|INF204KB15I9", "BANKBEES"),
    "LIQUIDBEES.NS" : ("NSE_EQ|INF732E01037", "LIQUIDBEES"),
    "ITBEES.NS"     : ("NSE_EQ|INF204KB15V2", "ITBEES"),
    "PHARMABEES.NS" : ("NSE_EQ|INF204KC1089", "PHARMABEES"),
    "INFRABEES.NS"  : ("NSE_EQ|INF732E01268", "INFRABEES"),
    "AUTOBEES.NS"   : ("NSE_EQ|INF204KC1337", "AUTOBEES"),
    "CPSEETF.NS"    : ("NSE_EQ|INF457M01133", "CPSEETF"),
    "HNGSNGBEES.NS" : ("NSE_EQ|INF204KB19I1", "HNGSNGBEES"),
    "MON100.NS"     : ("NSE_EQ|INF247L01AP3", "MON100"),
    "BTC-USD"       : (None, None),
}

# ── NSE PUBLIC SESSION (auto-cookie) ──────────────────────────────────────────
_nse_session = None

def get_nse_session():
    """NSE requires a session cookie before serving API responses."""
    global _nse_session
    if _nse_session is not None:
        return _nse_session
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        # First request to nseindia.com sets required cookies
        s.get("https://www.nseindia.com/", timeout=10)
        time.sleep(0.5)
        _nse_session = s
    except Exception as e:
        print(f"    [DEBUG] NSE session init failed: {e}")
    return _nse_session


def get_nse_live_price(symbol):
    """Get live ETF price from NSE public API (no auth required)."""
    s = get_nse_session()
    if s is None:
        return None
    try:
        url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
        r = s.get(url, timeout=10)
        if r.ok:
            data = r.json()
            ltp = data.get("priceInfo", {}).get("lastPrice")
            if ltp and float(ltp) > 0:
                return float(ltp)
    except Exception as e:
        print(f"    [DEBUG] NSE quote {symbol}: {e}")
    return None


def get_upstox_v2_ltp(instrument_key):
    """Try Upstox V2 LTP — works only with LIVE tokens, not sandbox."""
    try:
        enc = quote(instrument_key, safe="")
        url = f"{UPSTOX_BASE}/v2/market-quote/ltp?instrument_key={enc}"
        r   = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        if r.ok:
            data = r.json().get("data", {})
            for key, val in data.items():
                p = val.get("last_price")
                if p and float(p) > 0:
                    return float(p)
    except Exception:
        pass
    return None


def get_upstox_last_close(instrument_key):
    """V3 historical — works with sandbox tokens, returns last daily close."""
    to_date   = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
    encoded   = quote(instrument_key, safe="")
    url = (f"{UPSTOX_BASE}/v3/historical-candle"
           f"/{encoded}/days/1/{to_date}/{from_date}")
    try:
        r = requests.get(url, headers=AUTH_HEADERS, timeout=10)
        if r.ok:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                return float(candles[0][4])
    except Exception as e:
        print(f"    [DEBUG] Upstox V3 hist: {e}")
    return None


def live_price_inr(ticker):
    """
    Resolves the best available price for any ticker.
    Tries: Upstox live → NSE live → Upstox historical (last close)
    """
    print(f"\n  ➤ {ticker}")

    # ── BTC via CoinGecko (returns INR directly) ──
    if ticker == "BTC-USD":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=inr",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            val = float(r.json()["bitcoin"]["inr"])
            print(f"    CoinGecko: ₹{val:,.0f}")
            return val
        except Exception as e:
            print(f"    [WARN] CoinGecko: {e}")
            return None

    inst_key, nse_symbol = INSTRUMENT_MAP.get(ticker, (None, None))
    if not inst_key:
        return None

    # ── 1. Try Upstox V2 LTP (live tokens only) ──
    val = get_upstox_v2_ltp(inst_key)
    if val:
        print(f"    Upstox LTP: ₹{val:,.2f}")
        return val

    # ── 2. Try NSE public API (free, real-time during market hours) ──
    if nse_symbol:
        val = get_nse_live_price(nse_symbol)
        if val:
            print(f"    NSE live:   ₹{val:,.2f}")
            return val

    # ── 3. Fall back to last historical close ──
    val = get_upstox_last_close(inst_key)
    if val:
        print(f"    Last close: ₹{val:,.2f}")
        return val

    print(f"    [WARN] No price available")
    return None


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
    lp = live_price_inr(t)
    if lp is None or lp == 0:
        print(f"  [WARN] No price for {t}, skipping")
        continue
    inv      = cost_basis.get(t, 0.0)
    avg_cost = inv / qty if qty > 0 else 0
    cur      = qty * lp
    pnl      = cur - inv
    pnl_pct  = (pnl / inv * 100) if inv > 0 else 0.0

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

# ── Market status (IST) ──────────────────────────────────────────────────────
now_utc = datetime.utcnow()
ist_min_total = (now_utc.hour * 60 + now_utc.minute + 330) % (24 * 60)
weekday = (now_utc.weekday()) % 7  # Mon=0...Sun=6
market_open = (
    weekday < 5 and
    (9 * 60 + 15) <= ist_min_total <= (15 * 60 + 30)
)
market_status = "LIVE" if market_open else "CLOSED — last close shown"

# ── Write dashboard ──────────────────────────────────────────────────────────
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

print(f"\n{'='*55}")
print(f"Market: {market_status}")
print(f"P&L:     ₹{net_pnl:+,.0f} ({net_pnl_pct:+.2f}%)")
print(f"Inv:     ₹{total_inv:,.0f}  →  Cur: ₹{total_cur:,.0f}")
print(f"Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*55}")
