# RAAM Live — Automated Weekly Execution + Dashboard

## Free stack
| Component | Service | Cost |
|---|---|---|
| Weekly automation | GitHub Actions | Free (2000 min/month) |
| Dashboard hosting | GitHub Pages | Free |
| Data storage | This repo | Free |
| Price data | yfinance | Free |

---

## Setup (one-time, ~10 minutes)

### Step 1 — Create a GitHub repo

1. Go to github.com → New repository
2. Name it `raam-live`
3. Set to **Private** (keeps your trade data private)
4. Upload these files:
   - `raam_runner.py`
   - `requirements.txt`
   - `index.html`
   - `.github/workflows/raam_weekly.yml`
   - `data/stage_3/master_ensemble_signals.csv`  ← copy from your notebook folder
   - `data/stage_3/trade_log.csv`                ← copy if it exists, else skip

### Step 2 — Add secrets (your API keys, securely)

In your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these one by one:

| Secret name | Value |
|---|---|
| `UPSTOX_TOKEN` | Your 30-day sandbox access token |
| `PORTFOLIO_VALUE` | `60000` |
| `MODE` | `sandbox` |
| `BINANCE_API_KEY` | Your Binance API key (optional, for BTC) |
| `BINANCE_SECRET_KEY` | Your Binance secret (optional, for BTC) |

### Step 3 — Enable GitHub Pages

1. Repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: `/ (root)`
4. Click **Save**
5. Your dashboard URL: `https://{your-github-username}.github.io/raam-live/`

### Step 4 — Test the workflow manually

1. Repo → **Actions** tab
2. Click **RAAM Weekly Execution Engine**
3. Click **Run workflow** → **Run workflow**
4. Watch the logs — should take ~45 seconds
5. After it finishes, open your GitHub Pages URL to see the dashboard

---

## Weekly automation

The workflow runs **every Friday at 3:30 PM IST automatically** (no action needed from you).

It:
1. Downloads fresh price data via yfinance
2. Reads the pre-computed signal matrix from the repo
3. Computes this week's target allocation using RAAM hysteresis logic
4. Places orders via Upstox API (sandbox or live)
5. Updates `trade_log.csv` and `dashboard_data.json`
6. Commits everything back to the repo
7. GitHub Pages serves the updated dashboard

---

## Updating your token (every 30 days)

Sandbox tokens expire after 30 days. When it expires:
1. Go to account.upstox.com/developer/apps#sandbox
2. Click Generate to get a new token
3. In GitHub: Settings → Secrets → Update `UPSTOX_TOKEN`
4. Done — next run picks it up automatically

---

## Going live (when ready)

1. Create a live Upstox developer app at account.upstox.com/developer/apps
2. Update GitHub Secrets: `MODE=live`, new `UPSTOX_TOKEN` (live token)
3. Register your GitHub Actions IP with Upstox (SEBI requirement)
   - GitHub Actions runs on Microsoft Azure IPs
   - You need a static IP for live trading — for this use a VPS or Upstox's broker API delegation

Note: For true live trading with SEBI compliance, a ₹500/month VPS with a static IP is cleaner than GitHub Actions.
