# Live Mandi Price Advisor — Full Technical Build Plan

A farmer picks a state, then a crop, and sees a live price trend, a short-term forecast,
a **Sell Now / Hold** signal, and a comparison across nearby mandis. The app's UI is a
two-page flow: a **Home page** (intro, cross-state dashboard, feedback, team) and a
**Prediction page** (clickable India map → state → top 6 crops → per-mandi drill-down).
Full UI spec is in Section 7.

---

## 1. System Architecture (the big picture)

```
┌───────────────────────┐        ┌───────────────────────┐
│ Kaggle historical      │        │ data.gov.in Agmarknet  │
│ dataset (2024+2025     │        │ API (daily live prices)│
│ parquet — ONE-TIME     │        │                        │
│ backfill) — Section 3.3│        └───────────┬───────────┘
└───────────┬───────────┘                     │ daily, scheduled
            │ one-time load                    ▼
            │                        ┌───────────────────┐
            │                        │  Ingestion script  │◄──── (GitHub Actions cron)
            │                        │  (Python requests) │
            │                        └─────────┬─────────┘
            ▼                                  ▼
        ┌───────────────────────────────────────────┐
        │  Database — tables PARTITIONED BY STATE     │
        │  (state_maharashtra_prices, state_punjab_.. )│
        │  SQLite (dev) / Postgres (prod)              │
        └─────────────────────┬─────────────────────┘
                              ▼
                ┌───────────────────────────────┐   also runs daily,
                │  Retraining job                │◄── right after ingestion
                │  ONE Prophet model PER          │
                │  (commodity, mandi) pair —      │
                │  NOT per state (see Section 4.5)│
                └─────────────┬─────────────────┘
                              ▼
                ┌───────────────────┐
                │  Model store       │
                │  (joblib files +   │
                │   model_registry   │
                │   table)           │
                └─────────┬─────────┘
                          ▼
                ┌───────────────────────────────┐
                │  FastAPI backend                │  serves per-mandi forecasts,
                │  (REST endpoints, incl. a       │  state-level AGGREGATES
                │  state-aggregation endpoint)    │  computed on top of them
                └─────────────┬─────────────────┘
                              ▼
                ┌───────────────────────────────┐
                │  React frontend                 │  Home page + Prediction
                │  (map, charts, dashboards)       │  page — this is what the
                └───────────────────────────────┘  farmer actually sees
```

**Important honesty point for your report/viva:** Agmarknet mandis report prices
**once per day** (end of trading day), not tick-by-tick. So "real-time" here correctly
means "refreshed daily, automatically, without manual intervention" — not live streaming
data. This is normal and expected for commodity price systems; say this proactively in your
report so it doesn't look like a gap when an evaluator points it out.

**Key architectural decision (finalized):** state is a **storage and navigation**
concept — your database tables and your UI are organized by state. The **model**
itself stays at **(commodity, mandi)** granularity, never "per state." Section 4.5
explains exactly why, and how the two layers connect.

---

## 2. Tech Stack Summary

| Layer | Tool | Why |
|---|---|---|
| Historical backfill | **Kaggle "Daily Market Prices of Commodity India (2001–2026)"** — using the 2024.parquet + 2025.parquet slices already on hand | Same underlying official Agmarknet/Ministry of Agriculture data, pre-compiled into ready-to-load parquet by the same author who maintains the live API dataset used for daily ingestion — one consistent lineage across historical + live, and far simpler to bulk-filter (3 states × 6 crops) than CEDA's per-query export UI. CEDA dropped as a source (was awkward for bulk pulls across multiple state/commodity combinations at once) |
| Live data fetch | Python `requests` against data.gov.in Agmarknet API | Simple, no extra dependency |
| Scheduler | GitHub Actions (cron) *or* APScheduler | Free, no server needed to run 24/7 |
| Database | SQLite (local dev) → Supabase/Neon Postgres (deployed), tables **partitioned by state** | Matches the UI's state-first navigation; keeps per-state queries fast |
| Forecasting model | **Facebook Prophet**, one model per **(commodity, mandi)** | Handles small per-mandi datasets well, minimal tuning, gives trend + confidence intervals out of the box — see Section 4.5 for why granularity stays at mandi level, not state level |
| Baseline model | scikit-learn (moving average / linear regression) | Lets you *prove* Prophet is better — evaluators like seeing a comparison |
| Backend API | FastAPI | Lightweight, async, auto-generates API docs; also computes state-level aggregates on top of mandi-level forecasts |
| Frontend | **React** (Vite) | Needed for a real clickable India map + smooth two-page app; Streamlit isn't suited to interactive map-click navigation |
| Map (India, clickable states) | `react-simple-maps` + a India-states TopoJSON | Standard, well-documented way to build a clickable choropleth/click-map in React |
| Charts | `recharts` | React-native charting, good for price trend + forecast band charts |
| Routing | `react-router-dom` | Two pages: Home and Prediction |
| Styling | Tailwind CSS | Fast to build clean UI without hand-rolling CSS |
| Model persistence | `joblib` | Save/load trained Prophet models |
| Deployment | Vercel/Netlify (React frontend, free tier) + Render (FastAPI backend, free tier) + Supabase (Postgres, free tier) | All free, all standard for a student full-stack deployment |

---

## 3. Step 1 — Data Pipeline

### 3.0 Understanding the raw data first

Before writing any code, know exactly what the API gives you. Here's what each column means:

| Field (API name) | What it is | Notes |
|---|---|---|
| `state` / `district` / `market` | Location hierarchy | `market` is the actual mandi name (e.g., "Mukkom Market") — your key filter |
| `commodity` | The crop | e.g., Beetroot, Apple, Mango |
| `variety` | Sub-type of the commodity | Often identical to the commodity name for generic vegetables, but can genuinely differ for crops like Apple/Mango (e.g., a "Kashmir" variety) |
| `grade` | Quality tier | FAQ = "Fair Average Quality" (standard Agmarknet grading term), also Medium, Good, etc. |
| `arrival_date` | The date this price was recorded | Your time-series axis |
| `min_price` / `max_price` / `modal_price` | Price range for that day, in ₹ per quintal (100 kg) | See below for which one to use |

**Use `modal_price` as your forecast target — not an average of min/max.** Modal price
is the price at which the *largest quantity was actually transacted* that day — the
real representative trading price. Min/max only show the spread of individual trades.

**A naming quirk you'll see in the raw web preview (not the API response):** the
data.gov.in preview table shows headers like "Min X0020 Price" — that's an
XML-encoding artifact (`x0020` = a space character), not a real field name. The actual
JSON field names your code will use are the clean lowercase versions: `min_price`,
`max_price`, `modal_price`. Don't try to match the display labels in your code.

**A gotcha that will silently break your time series if you don't handle it:** a
single market can have *multiple rows on the same date* for the same commodity, if
more than one variety or grade was traded that day (e.g., two Apple varieties sold at
the same mandi on the same day, at different prices). If you don't account for this,
your "time series" for Prophet will jump between different varieties' prices from day
to day — which looks like noise the model can't explain, not a real price movement.

**Fix:** when building your per-(commodity, market) series, either:
- Filter to one consistent `variety` + `grade` combination (recommended — cleanest), or
- Aggregate multiple same-day rows (e.g., average) if you want broader coverage and can
  accept slightly less precision.

The schema and script below already store `variety`, so you can apply this filter when
you pull data for training.

### 3.1 Get your API key
1. Register (free) at https://www.data.gov.in
2. Go to "My Account" → Generate API Key
3. The dataset you need is **"Current Daily Price of Various Commodities from Various
   Markets (Mandi)"**, resource ID: `9ef84268-d588-465a-a308-a864a43d0070`

### 3.2 The actual API call
```
GET https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070
    ?api-key=YOUR_KEY
    &format=json
    &limit=1000
    &filters[state]=Maharashtra
    &filters[commodity]=Onion
```

Fields returned: `state, district, market, commodity, variety, grade, arrival_date,
min_price, max_price, modal_price`

### 3.3 Historical backfill — confirmed source (do this before anything else)
Prophet needs several months of history to detect seasonality properly (weekly patterns
need several weeks minimum; yearly/seasonal patterns need a year+ of history — you will
not get that from live collection alone within one academic semester). This is a hard
blocker for Phase 1, not a nice-to-have.

**Source (finalized) — Kaggle "Daily Market Prices of Commodity India (2001–2026)"**,
using the `2024.parquet` and `2025.parquet` slices already on hand. This is compiled by
the same original owner/maintainer as the live API dataset used for daily ingestion
(Section 3.4) — one consistent data lineage across historical backfill and live
collection, rather than stitching together two independently-compiled sources.
**CEDA Agri Market Data is no longer used** — its per-query export UI was awkward for
bulk-pulling three states × six commodities at once; the Kaggle parquet files give the
same underlying official Agmarknet data pre-filterable in bulk.

**How this connects to the live pipeline:** backfill is a **one-time load** that happens
once, before you turn on daily collection. Once your database has this historical base,
your daily ingestion script (Section 3.4) just keeps appending new days to the same
table going forward — this is the "accumulate day by day, like a stock price feed"
mechanism, and it is the correct ongoing behavior. It just cannot be the *only* source
of history, or your model will have no seasonality signal for months.

**Loader script:** `data_pipeline/backfill_kaggle.py` (replaces the old
`backfill_ceda.py`) — reads both parquet files, filters to the 3 finalized states and
18 finalized crops below, cleans/standardizes commodity name variants, and loads into
Postgres.

### 3.3.1 Finalized states + top-6-crops-per-state (data-driven, locked)
Derived from actual reporting-frequency evidence in the 2024+2025 Kaggle parquet data
(row/day consistency per commodity per state, not just raw row count — a crop reported
inconsistently leaves gaps in Prophet training data even with a large one-off row
count). This is the list that powers the Prediction page's "click a state → see its top
6 crops" screen (Section 7).

| State | Top 6 crops (ranked) |
|---|---|
| **Tamil Nadu** *(combined 2024+2025 ranking, for robustness)* | 1. Coconut · 2. Bhindi (Ladies Finger) · 3. Green Chilli · 4. Bottle gourd · 5. Snakeguard · 6. Onion *(edged out Banana-Green by only ~1,000 records — a near-tie worth knowing)* |
| **Uttar Pradesh** *(stable both years independently)* | 1. Potato · 2. Onion · 3. Tomato · 4. Wheat · 5. Brinjal · 6. Green Chilli |
| **Maharashtra** *(stable both years independently)* | 1. Wheat · 2. Bengal Gram · 3. Soyabean · 4. Arhar (Tur/Red Gram) · 5. Onion · 6. Jowar (Sorghum) |

This is now the fixed scope for Phase 1–3 (18 total commodity-state pairs, further
split per mandi for actual model training — Section 4.5). The `state_top_crops` config
table (Section 9) should be seeded directly from this table rather than recomputed, since
it's already been derived from evidence.

**Still open — the Variety question (not yet decided):** several of these commodities
(e.g., Onion, Green Chilli) have multiple reported varieties per mandi/day (Section
3.0's gotcha). Whether to collapse varieties per commodity into one series, or train
separate models per variety, is still an open architectural call — it changes the total
model count and how granular the per-mandi signal actually is. Resolve this before
Section 4 (ML Core) training begins.

### 3.4 Ingestion script (concept)
```python
# fetch_agmarknet.py
import requests, sqlite3
from datetime import date

API_KEY = "YOUR_KEY"
RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"
BASE_URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"

def fetch(state, commodity, limit=1000):
    params = {
        "api-key": API_KEY, "format": "json", "limit": limit,
        "filters[state]": state, "filters[commodity]": commodity,
    }
    r = requests.get(BASE_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["records"]

def save_to_db(records, db_path="mandi.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS mandi_prices (
        state TEXT, district TEXT, market TEXT, commodity TEXT, variety TEXT,
        arrival_date TEXT, min_price REAL, max_price REAL, modal_price REAL,
        fetched_at TEXT, UNIQUE(market, commodity, variety, arrival_date))""")
    # variety is part of the uniqueness key deliberately — see section 3.0:
    # the same market/commodity/date can have multiple rows across varieties
    for rec in records:
        conn.execute("""INSERT OR IGNORE INTO mandi_prices VALUES
            (?,?,?,?,?,?,?,?,?,?)""",
            (rec["state"], rec["district"], rec["market"], rec["commodity"],
             rec.get("variety"), rec["arrival_date"], rec.get("min_price"),
             rec.get("max_price"), rec.get("modal_price"), str(date.today())))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    for commodity in ["Onion", "Tomato", "Wheat", "Potato", "Soyabean"]:
        records = fetch("Maharashtra", commodity)   # scope: 1 state, 5 crops to start
        save_to_db(records)
```

### 3.5 Scheduling it
**Easiest for students — GitHub Actions** (no server needed, completely free):

```yaml
# .github/workflows/daily_pipeline.yml
name: Daily Mandi Pipeline
on:
  schedule:
    - cron: '30 14 * * *'   # ~8:00 PM IST, after mandis close
  workflow_dispatch: {}      # lets you trigger it manually too
jobs:
  run-pipeline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: python data_pipeline/fetch_agmarknet.py
        env:
          DATA_GOV_API_KEY: ${{ secrets.DATA_GOV_API_KEY }}
          DB_URL: ${{ secrets.SUPABASE_DB_URL }}
      - run: python ml/retrain.py
        env:
          DB_URL: ${{ secrets.SUPABASE_DB_URL }}
```
Store keys as GitHub repo **Secrets**, never hardcode them in the script.

---

## 4. Step 2 — ML Core

### 4.0 What kind of ML model is this? (define this precisely for your report)

This project's core ML task is **regression** — specifically **time-series
forecasting** — not classification. You are predicting a continuous number (the
future price), not a category, even though the dashboard displays a category-looking
"Sell Now / Hold" badge.

| Component | ML type | Model |
|---|---|---|
| Price forecast (the actual ML model) | **Regression** (time-series forecasting) | Prophet — a Generalized Additive Model (trend + weekly seasonality + yearly seasonality) |
| Baseline for comparison | Regression | Naive/moving-average, linear regression |
| Sell Now / Hold / Stable badge | **Not ML** — a rule-based decision layer | Threshold logic applied to the regression output |

**Why the Sell/Hold badge isn't a classifier, and why that's the right design:** you
could train a 3-class classifier directly on "will price rise/fall/stay flat," but
predicting the actual price first and applying a threshold rule afterward is the
better design — it gives the farmer the real predicted number and a confidence band
("price expected to rise ~4% by Thursday"), not just a bare label. This is the
standard architecture for advisory/signal systems generally, not a shortcut.

**One-line answer for your report/viva:**
> "This project uses time-series regression (an additive regression model — Prophet)
> to forecast future mandi prices, with a rule-based decision layer that converts the
> continuous forecast into an actionable Sell/Hold recommendation."

**Optional stretch (good for a "model comparison" section):** you could additionally
train a direct classifier (XGBoost/Random Forest) on engineered features (recent
trend, volatility, day-of-week, season) predicting the label directly, then compare its
accuracy against your regression-derived signal. Not required, but shows breadth if
you have spare time.

### 4.1 Feature/data prep
Per (commodity, mandi, **variety**) triple, build a time series of `(date, modal_price)`.
Filtering to one consistent variety matters — see section 3.0's gotcha about multiple
varieties per market/day. Prophet wants exactly two columns named `ds` and `y`:

```python
df = df[df["variety"] == chosen_variety]          # avoid mixing varieties, see 3.0
df = df.rename(columns={"arrival_date": "ds", "modal_price": "y"})
df["ds"] = pd.to_datetime(df["ds"])
```

Optional extra regressors if you want to go further: `min_price`, `max_price` spread
(a proxy for volatility), day-of-week (Prophet already models weekly seasonality
automatically).

### 4.2 Training
```python
from prophet import Prophet

model = Prophet(
    daily_seasonality=False,
    weekly_seasonality=True,
    yearly_seasonality=True,
    interval_width=0.85          # confidence band for your sell/hold logic
)
model.fit(df[["ds", "y"]])

future = model.make_future_dataframe(periods=7)   # forecast next 7 days
forecast = model.predict(future)
```

### 4.3 Baseline comparison (do this — evaluators like it)
```python
from sklearn.metrics import mean_absolute_error
naive_forecast = df["y"].iloc[-1]   # "tomorrow = today" baseline
prophet_mae = mean_absolute_error(actual_holdout, prophet_predictions)
naive_mae = mean_absolute_error(actual_holdout, [naive_forecast]*len(actual_holdout))
# report: "Prophet reduced forecast error by X% over the naive baseline"
```

### 4.4 The Sell Now / Hold logic
This is a simple, explainable rule layered on top of the forecast — don't overcomplicate it,
explainability matters more than cleverness here:

```python
current_price = df["y"].iloc[-1]
predicted_peak = forecast["yhat"].tail(7).max()
predicted_change_pct = (predicted_peak - current_price) / current_price * 100
band_width = (forecast["yhat_upper"] - forecast["yhat_lower"]).tail(7).mean()

if band_width / current_price > 0.15:
    signal = "LOW CONFIDENCE — prices volatile, use judgement"
elif predicted_change_pct > 3:
    signal = "HOLD — price likely to rise over next week"
elif predicted_change_pct < -3:
    signal = "SELL NOW — price likely to fall"
else:
    signal = "STABLE — sell if you need liquidity, minor difference either way"
```

### 4.5 Data organization vs. model granularity (important distinction — finalized decision)

**The mistake to avoid:** training one Prophet model per (commodity, **state**) instead
of per (commodity, **mandi**). A state is not one marketplace — it's many mandis bundled
under one label, and prices genuinely differ mandi to mandi within the same state on the
same day (e.g., Onion at Lasalgaon vs. Onion at Solapur, both Maharashtra, same day, can
differ by hundreds of rupees per quintal due to local supply/arrivals). A state-level
model averages these together and gives every farmer in that state the same blurred
number — which defeats the project's original purpose (comparing *nearby* mandis to
avoid distress selling).

**The correct split, finalized:**
- **Storage layer:** database tables are partitioned/organized **by state** (matches
  Section 3.3.1's per-state crop lists and the Prediction page's state-first navigation).
- **Model layer:** unchanged from Section 4.0 — **one Prophet model per (commodity,
  mandi) pair.** This does not change based on how you organize storage.
- **State-level UI screen:** the "click a state, see its top 6 crops with % change"
  view (Section 7) does **not** need its own model. It's an **aggregation computed on
  top of the existing mandi-level forecasts** — cheap arithmetic, not new ML.

**How the aggregation works (pseudocode, lives in the backend, Section 6):**
```python
def get_state_summary(state, top_6_commodities):
    summary = []
    for commodity in top_6_commodities:
        mandi_forecasts = []
        for market in markets_in_state(state, commodity):
            model = load_model(commodity, market)          # per-mandi model, unchanged
            forecast = model.predict(next_7_days())
            pct_change = compute_pct_change(forecast)        # same logic as 4.4
            mandi_forecasts.append(pct_change)

        # state tile shows the AVERAGE across that state's reporting mandis —
        # a summary of underlying mandi-level forecasts, not a separately trained model
        summary.append({
            "commodity": commodity,
            "avg_pct_change": mean(mandi_forecasts),
            "mandi_count": len(mandi_forecasts)
        })
    return summary
```
When the farmer drills into one crop from the state screen, that's where the **real
per-mandi breakdown** (map, individual forecasts, Sell/Hold per mandi) is shown — using
the actual mandi-level models. The state screen is a summary/overview layer; the mandi
drill-down is where the real precision (and the original project brief) lives.

---

## 5. Step 3 — How the model "updates itself on real-time data" (the part you asked about)

This is worth understanding properly, since it's a strong viva question.

**There are two different things people mean by "the model updates itself":**

1. **True online learning** — the model's internal weights are updated incrementally,
   one data point at a time, as data streams in continuously (used for things like
   click-prediction or sensor data arriving every second). Libraries like `river` do this.
2. **Scheduled batch retraining** — the model is *refit from scratch* on the full,
   growing dataset on a regular schedule (e.g., nightly).

**For daily mandi prices, #2 is the correct approach — not #1.** Since new ground-truth
data only arrives once per day per mandi, there's nothing to "stream" hourly; doing
online learning here would be over-engineering for no benefit. Real production
forecasting systems for daily/weekly-granularity data (retail demand, commodity pricing)
almost universally use scheduled retraining, not tick-level online learning. Say this
explicitly in your report — it shows you understood the tradeoff rather than just
picked the fancier-sounding option.

### The retrain pipeline (runs automatically, same schedule as ingestion)
```
Ingest new day's prices
        ↓
Validate (check for missing days / anomalous price spikes)
        ↓
Refit Prophet model on full updated history, per (commodity, mandi)
        ↓
Evaluate: compare new model's holdout MAE to yesterday's model's MAE
        ↓
Promote: only replace the "live" model if the new one isn't worse
        ↓
Log to model_registry table
```

### A lightweight "model registry" (this is a nice professional touch)
```sql
CREATE TABLE model_registry (
    commodity TEXT, market TEXT, model_path TEXT,
    trained_at TIMESTAMP, mae_score REAL, is_active BOOLEAN
);
```
```python
# retrain.py (simplified)
import joblib

def retrain_and_promote(commodity, market, df):
    new_model = train_prophet(df)
    new_mae = evaluate(new_model, df)
    current = get_active_model(commodity, market)  # query model_registry

    if current is None or new_mae <= current.mae_score:
        path = f"models/{commodity}_{market}.pkl"
        joblib.dump(new_model, path)
        promote_in_registry(commodity, market, path, new_mae)  # sets is_active=True
    # else: keep serving yesterday's model, log that retrain was skipped
```
Your FastAPI backend simply always loads whichever model is currently flagged
`is_active = True` for that (commodity, market) — it never needs to know *how* the
model got there.

**Good "future work" line for your report:** if arrival-volume or auction-level
intraday data ever becomes available, the same pipeline could be extended with a
`river`-based online layer for intraday adjustment — but it isn't needed for the
data granularity you actually have, so you scoped it correctly for this project.

---

## 6. Step 4 — Backend (FastAPI)

```python
# backend/main.py
from fastapi import FastAPI
import joblib, sqlite3

app = FastAPI()

@app.get("/states")
def list_states():
    ...  # DISTINCT state — powers the clickable India map on the Prediction page

@app.get("/states/{state}/top-crops")
def top_crops(state: str):
    ...  # reads the precomputed state_top_crops table (Section 3.3.1)

@app.get("/states/{state}/summary")
def state_summary(state: str):
    # aggregates existing per-mandi models — see Section 4.5 for why this is NOT
    # a separately trained model, just arithmetic over mandi-level forecasts
    top_6 = get_top_crops(state)
    return compute_state_summary(state, top_6)

@app.get("/commodities")
def list_commodities():
    ...  # DISTINCT commodity from mandi_prices

@app.get("/markets")
def list_markets(commodity: str, state: str):
    ...  # DISTINCT market WHERE commodity=... AND state=...

@app.get("/forecast")
def get_forecast(commodity: str, market: str):
    model = joblib.load(f"models/{commodity}_{market}.pkl")   # per-mandi model, unchanged
    future = model.make_future_dataframe(periods=7)
    forecast = model.predict(future)
    return {
        "forecast": forecast[["ds","yhat","yhat_lower","yhat_upper"]].tail(7).to_dict("records"),
        "signal": compute_sell_hold_signal(...)
    }

@app.get("/compare")
def compare_mandis(commodity: str, state: str):
    ...  # latest modal_price per market within that state, for the drill-down map view
```
Run locally with `uvicorn backend.main:app --reload`. FastAPI gives you free
interactive API docs at `/docs` — useful to demo to evaluators directly.

---

## 7. Step 5 — Frontend (React) — two pages

React was chosen over Streamlit specifically because the Prediction page needs a real
clickable India map, which Streamlit isn't well suited for (Section 2). Both pages
below match the UI vision you laid out, with the state/mandi model split from Section
4.5 built in from the start.

### 7.1 Home page
```
frontend/src/pages/Home.jsx
```
Sections, top to bottom, each a separate component for maintainability:

1. **Hero** — project intro + a catchy line + a prominent "Check Today's Prediction"
   button that routes (`react-router-dom`) to `/prediction`.
2. **All-India dashboard** — a dynamic cross-state overview showing crop-price
   relationships and mandi info as relational tables/small charts (`recharts`). This
   pulls from a general "today's overview" backend endpoint (e.g. top movers across all
   states) — genuinely dynamic, refreshed from the live database, not static content.
3. **Feedback section** — display-only for the demo. **State this explicitly to
   evaluators**: "wired to accept real user feedback post-deployment; populated with
   sample data for this demo" — say it up front rather than let it look like an
   oversight.
4. **Team section** — team member icons/photos, each linking out to that person's
   GitHub profile (`<a href="https://github.com/username" target="_blank">`).

### 7.2 Prediction page
```
frontend/src/pages/Prediction.jsx
```
Two-pane layout:

- **Left pane — clickable India map.** Built with `react-simple-maps` + an
  India-states TopoJSON file. Each state is a clickable shape; clicking sets the
  selected state in React state (`useState`) and triggers a fetch to
  `/states/{state}/top-crops` and `/states/{state}/summary`.
- **Right pane — top 6 crops for the selected state**, each shown as a card with:
  the commodity name, the **aggregated** % change (Section 4.5 — averaged across that
  state's reporting mandis, not a separately trained state model), and an up/down
  indicator (color-coded, e.g. green for price rising / good time to hold, red for
  falling / consider selling).
- **Drill-down (click a crop card):** navigates to a detail view showing the *real*
  per-mandi breakdown for that (state, commodity) — a small map or table comparing
  mandis within that state (`/compare` endpoint), each with its own Sell/Hold badge
  from its own individually-trained model (Section 4.4). This is where the original
  "compare nearby mandis" feature actually lives — the state screen before it is just
  a summary layer.

### 7.3 Suggested component structure
```
frontend/src/
├── pages/
│   ├── Home.jsx
│   └── Prediction.jsx
├── components/
│   ├── Hero.jsx
│   ├── AllIndiaDashboard.jsx
│   ├── FeedbackSection.jsx
│   ├── TeamSection.jsx
│   ├── IndiaMap.jsx            # react-simple-maps wrapper
│   ├── StateCropCard.jsx       # one of the top-6 crop cards
│   ├── MandiCompareView.jsx    # drill-down: per-mandi map/table + Sell/Hold
│   └── ForecastChart.jsx       # recharts price + forecast band chart
├── api/
│   └── client.js               # fetch wrappers to the FastAPI backend
└── App.jsx                     # react-router-dom routes
```

This satisfies your original brief well: live data → interactive dashboard that
*explains* the prediction → usable by a non-technical farmer, now matching your actual
two-page UI vision end to end.

**Mandi coordinates note:** the Agmarknet dataset doesn't include lat/long. For the
~20–30 mandis you scope to per state, build a small static CSV mapping market name →
coordinates once (geocode manually or via a free geocoding API) rather than trying to
solve this for all 3,000+ mandis. The India-states TopoJSON for the left-pane map is a
separate, off-the-shelf file — you don't need to build that part yourself.

---

## 8. Step 6 — Deployment (all free tiers)

| What | Where |
|---|---|
| Database | Supabase (free Postgres, ~500MB), tables partitioned by state (Section 4.5) |
| Ingestion + retraining schedule | GitHub Actions (cron, free minutes) |
| Backend API | Render free web service (FastAPI — needed as a real API layer since React calls it directly, not optional the way it was under the Streamlit-only MVP option) |
| Frontend | Vercel or Netlify (free, connects directly to your GitHub repo, auto-deploys the React app) |
| Secrets | GitHub Secrets (pipeline) / environment variables in Render + Vercel dashboards — never commit API keys |

**Note on scope:** with React in the picture, the FastAPI backend is no longer
optional/skippable the way it briefly was under the earlier Streamlit-only plan — the
React frontend needs a real API to call from day one. Budget for this in Phase 1
planning (Section 10), not as a later "nice to have."

---

## 9. Suggested repo structure
```
mandi-price-advisor/
├── data_pipeline/
│   ├── backfill_kaggle.py     # one-time historical load from 2024/2025 parquet (Section 3.3)
│   ├── fetch_agmarknet.py     # daily live ingestion
│   └── db.py
├── ml/
│   ├── train.py
│   ├── retrain.py
│   ├── select_top_crops.py    # Section 3.3.1 — data-driven top-6-per-state
│   └── models/                 # saved .pkl files, one per (commodity, mandi), gitignored
├── backend/
│   └── main.py                 # FastAPI — required (Section 8), serves per-mandi
│                                 # forecasts + state-level aggregation endpoints
├── frontend/                   # React app (Section 7)
│   ├── src/
│   │   ├── pages/
│   │   ├── components/
│   │   └── api/
│   └── package.json
├── .github/workflows/
│   └── daily_pipeline.yml
├── requirements.txt
└── README.md
```

---

## 10. Phased Roadmap

| Phase | Weeks | Deliverable |
|---|---|---|
| 1 | 1–2 | API key working, live ingestion script, DB schema **partitioned by state**, **Kaggle parquet historical backfill** loaded for the 3 finalized states (Tamil Nadu, Uttar Pradesh, Maharashtra) |
| 2 | 3 | Data-driven **top-6-crops-per-state** selection (Section 3.3.1), stored in `state_top_crops` |
| 3 | 4 | Prophet model + naive baseline **per (commodity, mandi)**, MAE comparison report |
| 4 | 5 | Sell/Hold logic finalized, tested against historical "what would we have told the farmer" cases |
| 5 | 6 | Retraining pipeline + model registry; state-aggregation logic (Section 4.5) built and unit-tested |
| 6 | 7 | FastAPI backend: per-mandi endpoints + state-summary/top-crops endpoints |
| 7 | 8 | React frontend: Home page (hero, all-India dashboard, feedback, team) |
| 8 | 9 | React frontend: Prediction page (clickable India map → top 6 crops → per-mandi drill-down) |
| 9 | 10 | Automate via GitHub Actions, deploy (Vercel + Render + Supabase) |
| 10 | 11–12 | Polish, documentation, report, presentation rehearsal |

**Scope control (finalized):** **3 states — Tamil Nadu, Uttar Pradesh, Maharashtra —
6 data-driven crops each (18 total, Section 3.3.1), ~15–20 mandis per state**. Expanding
coverage later is trivial (just loop the same code over more filters/states) — don't try
to cover all of India on day one.

---

## 11. Anticipated viva questions (prepare these answers now)

- **"Why Prophet instead of LSTM?"** → Per-mandi datasets are small; Prophet needs far
  less tuning, handles missing days gracefully, and gives interpretable trend +
  seasonality + confidence intervals out of the box. LSTM would need much more data
  per series and is prone to overfitting at this scale.
- **"Is this actually real-time?"** → It's refreshed daily, matching the real reporting
  frequency of mandi data (prices are set once per trading day at each mandi) — daily
  automatic refresh is the correct and honest definition of "real-time" for this
  domain.
- **"How do you know the model didn't get worse after retraining?"** → The
  champion/challenger check in the retrain pipeline: a new model only replaces the
  live one if its holdout error is no worse.
- **"How is this different from just checking Agmarknet's website?"** → Agmarknet
  shows current/historical prices; it doesn't forecast or give an actionable
  sell/hold recommendation — that's the gap this project fills.
- **"Why not just train one model per state, since your UI is state-first?"** → State
  is a navigation/storage convenience, not a real marketplace — prices genuinely differ
  mandi to mandi within a state. One model per (commodity, mandi) preserves that local
  accuracy; the state-level screen is a lightweight aggregation over those mandi-level
  forecasts, not a separate model (Section 4.5). This also directly serves the original
  goal of comparing *nearby* mandis, which a state-averaged model couldn't do.
- **"Why React instead of Streamlit if Streamlit is faster to build?"** → The
  Prediction page needs a genuinely interactive clickable India map with per-state
  drill-down, which isn't a natural fit for Streamlit. React (with `react-simple-maps`)
  gives real map-click interaction and a proper two-page app matching the actual
  product vision, at the cost of more manual setup than Streamlit's batteries-included
  approach — a deliberate tradeoff, not an oversight.
