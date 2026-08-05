# Live Mandi Price Advisor 🌾

Real-time mandi price forecasting & Sell/Hold advisory for Indian farmers — a BTech
CSE (AI/ML) final year capstone project.

A farmer picks a state, then a crop, and sees a live price trend, a short-term
forecast, a **Sell Now / Hold** signal, and a comparison across nearby mandis —
built to reduce distress selling caused by farmers not knowing real-time prices
across nearby markets.

## Problem this solves

Small and marginal farmers routinely sell produce without knowing its real worth,
often to middlemen at below-market rates, or too early due to a lack of storage.
This app closes that information gap with live, forecasted, comparable mandi prices.

## Live demo

_Links will be added here once deployed._

## Tech stack

| Layer | Tech |
|---|---|
| Historical backfill | Kaggle "Daily Market Prices of Commodity India" (2024+2025 parquet) |
| Live data source | data.gov.in Agmarknet API |
| Forecasting model | Facebook Prophet — one model per (commodity, mandi) |
| Backend | FastAPI |
| Database | SQLite (dev) / Supabase Postgres (prod), tables partitioned by state |
| Frontend | React (Vite) + react-simple-maps + recharts + Tailwind CSS |
| Deployment | Vercel/Netlify (frontend), Render (backend), Supabase (DB) |
| Scheduling | GitHub Actions (daily ingestion + retraining) |

Full architecture, data pipeline, ML design, and UI spec: **[docs/build-plan.md](docs/build-plan.md)**

## Project structure

```
live-mandi-price-advisor/
├── data_pipeline/      # historical backfill + daily live ingestion
├── ml/                 # Prophet training, retraining, crop selection
│   └── models/         # saved per-(commodity, mandi) model files (gitignored)
├── backend/             # FastAPI app
├── frontend/            # React app (Home page + Prediction page)
├── .github/workflows/    # scheduled data pipeline (GitHub Actions)
└── docs/                 # full technical build plan
```

## Getting started (backend)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# copy .env.example to .env and add your data.gov.in API key
cp .env.example .env

uvicorn backend.main:app --reload
```

## Getting started (frontend)

```bash
cd frontend
npm install
npm run dev
```

## Environment variables

See `.env.example`. You'll need a free API key from
[data.gov.in](https://www.data.gov.in) (My Account → Generate API Key) for the
Agmarknet dataset (resource ID `9ef84268-d588-465a-a308-a864a43d0070`).

## Team

| | |
|---|---|
| [Tanmoy Maiti] | [GitHub](https://github.com/) |
| [Arnab Das] | [GitHub](https://github.com/) |
| [Raja Kumar] | [GitHub](https://github.com/) |
| [Aditya Bharati] | [GitHub](https://github.com/) |

## License

MIT — see [LICENSE](LICENSE).
