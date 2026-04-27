# RAAM Live — Automated Weekly Execution + Dashboard

**100% free. Zero ongoing cost. Runs itself every Friday.**

| Component | Service | Cost |
|---|---|---|
| Weekly automation | GitHub Actions | Free (uses ~8 min/month of 2000 free) |
| Dashboard hosting | GitHub Pages | Free forever |
| Price data | yfinance | Free |
| ETF orders | Upstox API | Free (brokerage on trades only) |
| BTC orders | Binance API | Free (commission on trades only) |

---

## One-time setup (~10 minutes)

### Step 1 — Create a private GitHub repo

1. github.com → New repository → name it `raam-live` → **Private**
2. Upload ALL these files, keeping the folder structure:
   ```
   raam-live/
   ├── raam_runner.py
   ├── requirements.txt
   ├── index.html
   ├── dashboard_data.json          ← placeholder, gets updated by workflow
   ├── README.md
   ├── .github/
   │   └── workflows/
   │       └── raam_weekly.yml
   └── data/
       └── stage_3/
           └── master_ensemble_signals.csv   ← COPY FROM YOUR NOTEBOOK FOLDER
   ```
3. `master_ensemble_signals.csv` is in your notebook folder at `data/stage_3/`. Copy it to the repo.

### Step 2 — Add GitHub Secrets (your credentials, stored securely)

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret Name | Value | Required? |
|---|---|---|
| `UPSTOX_TOKEN` | Your Upstox sandbox/live access token | ✅ Always |
| `PORTFOLIO_VALUE` | e.g. `60000` | ✅ Always |
| `MODE` | `sandbox` (paper) or `live` (real money) | ✅ Always |
| `BINANCE_API_KEY` | Your Binance API key | Only if you want BTC orders |
| `BINANCE_SECRET_KEY` | Your Binance secret key | Only if you want BTC orders |

> **BTC note:** If you don't add Binance keys, BTC is silently skipped and its  
> weight goes to LIQUIDBEES (cash). Everything else still works perfectly.

### Step 3 — Enable GitHub Pages

1. Repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: `/ (root)`
4. Save → your dashboard is live at: `https://{your-username}.github.io/raam-live/`

### Step 4 — Test it right now (manual run)

1. Repo → **Actions** tab
2. Click **RAAM Weekly Execution Engine** → **Run workflow** → **Run workflow**
3. Watch the logs (~45 seconds)
4. Open your GitHub Pages URL — you should see the dashboard

---

## How it works every Friday

```
3:30 PM IST  →  GitHub Actions wakes up automatically
               Downloads fresh prices via yfinance
               Reads master_ensemble_signals.csv from repo
               Applies RAAM hysteresis momentum ranking
               Places ETF orders via Upstox API (AMO → executes Monday 9:15 AM)
               Places BTC order via Binance API (if keys provided)
               Updates dashboard_data.json
               Commits changes back to repo
               GitHub Pages serves updated dashboard
You           →  Open the dashboard link any time to see your portfolio
```

---

## Keeping your token fresh

Upstox sandbox tokens expire every 30 days.

1. Go to `account.upstox.com/developer/apps#sandbox`
2. Click **Generate** to get a new token
3. GitHub: Settings → Secrets → `UPSTOX_TOKEN` → **Update**
4. Done — next run picks it up automatically

When you go live, generate a live token the same way from your live app.

---

## Going live checklist

- [ ] 4+ weeks of paper trading validated
- [ ] Created live Upstox developer app
- [ ] Static IP registered with Upstox (SEBI requirement)
  - Cheapest: DigitalOcean/AWS Lightsail VPS at ₹500/month
  - For live trading, run the script from the VPS, not GitHub Actions
    (GitHub Actions IPs change every run and can't be whitelisted)
- [ ] Changed `MODE` secret to `live`
- [ ] Updated `UPSTOX_TOKEN` secret to live token
- [ ] Start with small capital (₹50,000–1,00,000)
