import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  ComposedChart,
  CartesianGrid,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";
import { getForecast, getHistory, getStateSummary, getTopCrops } from "../api";

const STATES = ["Tamil Nadu", "Uttar Pradesh", "Maharashtra"];

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `₹${number.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function errorMessage(error, fallback) {
  return error.response?.data?.detail || error.message || fallback;
}

function getForecastRows(payload) {
  return Array.isArray(payload?.forecast) ? payload.forecast : [];
}

function getHistoryRows(payload) {
  return Array.isArray(payload?.history) ? payload.history : [];
}

export default function Forecast() {
  const [step, setStep] = useState(1);
  const [state, setState] = useState("");
  const [crops, setCrops] = useState([]);
  const [crop, setCrop] = useState("");
  const [summary, setSummary] = useState(null);
  const [market, setMarket] = useState("");
  const [history, setHistory] = useState([]);
  const [forecast, setForecast] = useState(null);
  const [view, setView] = useState("chart");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!state) return;
    let cancelled = false;
    async function loadCrops() {
      setLoading(true);
      setError("");
      try {
        const result = await getTopCrops(state);
        const list = Array.isArray(result) ? result : result?.top_crops || result?.crops || [];
        if (!cancelled) setCrops(list);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err, "Could not load crops."));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadCrops();
    return () => { cancelled = true; };
  }, [state]);

  const markets = useMemo(() => {
    const rows = Array.isArray(summary?.forecasts) ? summary.forecasts : [];
    return [...new Set(rows.filter((row) => row.commodity === crop).map((row) => row.market).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
  }, [summary, crop]);

  async function selectCrop(value) {
    setCrop(value);
    setMarket("");
    setForecast(null);
    setHistory([]);
    setError("");
    setStep(3);
    setLoading(true);
    try {
      const result = await getStateSummary(state);
      setSummary(result);
    } catch (err) {
      setSummary(null);
      setError(errorMessage(err, "Could not load markets for this crop."));
    } finally {
      setLoading(false);
    }
  }

  async function selectMarket(value) {
    setMarket(value);
    setView("chart");
    setError("");
    setLoading(true);
    setStep(4);
    try {
      const [historyResult, forecastResult] = await Promise.all([
        getHistory(crop, value),
        getForecast(crop, value)
      ]);
      setHistory(getHistoryRows(historyResult));
      setForecast(forecastResult);
    } catch (err) {
      setHistory([]);
      setForecast(null);
      setError(errorMessage(err, "Could not load the history and forecast for this market."));
    } finally {
      setLoading(false);
    }
  }

  function goToStateSelection() {
    setStep(1);
    setState("");
    setCrops([]);
    setCrop("");
    setSummary(null);
    setMarket("");
    setHistory([]);
    setForecast(null);
    setError("");
  }

  function goToCropSelection() {
    setStep(2);
    setCrop("");
    setSummary(null);
    setMarket("");
    setHistory([]);
    setForecast(null);
    setError("");
  }

  function goToMarketSelection() {
    setStep(3);
    setMarket("");
    setHistory([]);
    setForecast(null);
    setError("");
  }

  const forecastRows = getForecastRows(forecast);
  const signal = String(forecast?.signal || "").toUpperCase();
  const chartData = useMemo(() => {
    const historical = history.map((row) => ({
      date: row.date,
      price: row.price,
      predicted_price: null,
      confidence_band: null
    }));
    const predicted = forecastRows.map((row) => ({
      date: row.date,
      price: null,
      predicted_price: row.predicted_price,
      confidence_band: [row.lower_bound, row.upper_bound]
    }));
    return [...historical, ...predicted].sort((a, b) => a.date.localeCompare(b.date));
  }, [history, forecast]);

  const firstForecastDate = forecastRows[0]?.date;

  return (
    <main className="forecast-page">
      <header className="forecast-header">
        <Link className="brand" to="/">Live Mandi Price Advisor</Link>
        <Link className="back-link" to="/">Home</Link>
      </header>

      <div className="history-banner" role="note">
        <strong>Historical demonstration</strong>
        <span>Data as of 29 Dec 2025 · Not live market data.</span>
      </div>

      <section className="forecast-main">
        <p className="eyebrow">MANDI PRICE EXPLORER</p>
        <h1>Explore a market forecast</h1>
        <nav className="breadcrumbs" aria-label="Breadcrumb">
          <button type="button" onClick={goToStateSelection} className={step === 1 ? "current" : ""}>All States</button>
          {state && <><span>›</span><button type="button" onClick={goToCropSelection} className={step === 2 ? "current" : ""}>{state}</button></>}
          {crop && <><span>›</span><button type="button" onClick={goToMarketSelection} className={step === 3 ? "current" : ""}>{crop}</button></>}
          {market && <><span>›</span><span className="breadcrumb-current">{market}</span></>}
        </nav>

        {step === 1 && (
          <section className="flow-section">
            <h2>Step 1 — Select your state</h2>
            <div className="state-select-grid">
              {STATES.map((item, index) => (
                <button className="state-select-card" type="button" key={item} onClick={() => {
                  setState(item);
                  setCrop("");
                  setSummary(null);
                  setMarket("");
                  setForecast(null);
                  setHistory([]);
                  setStep(2);
                }}>
                  <span className="state-index">0{index + 1}</span>
                  <strong>{item}</strong>
                  <span className="select-arrow">→</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {step >= 2 && (
          <section className="flow-section">
            <h2>Step 2 — Choose a crop</h2>
            {loading && step === 2 ? <p className="status-text">Loading crops…</p> : (
              <div className="crop-tabs">
                {crops.map((item) => (
                  <button type="button" key={item} className={`crop-tab ${crop === item ? "active" : ""}`}
                    onClick={() => selectCrop(item)}>{item}</button>
                ))}
              </div>
            )}
          </section>
        )}

        {step >= 3 && (
          <section className="flow-section">
            <h2>Step 3 — Pick your mandi</h2>
            {loading && step === 3 ? <p className="status-text">Loading markets…</p> : (
              <label className="market-select-label">
                <span>Select a market</span>
                <select value={market} onChange={(event) => event.target.value && selectMarket(event.target.value)}>
                  <option value="">Select a market...</option>
                  {markets.map((item) => <option value={item} key={item}>{item}</option>)}
                </select>
              </label>
            )}
            {!loading && markets.length === 0 && !error && <p className="status-text">No markets with forecast records were found for this crop.</p>}
          </section>
        )}

        {step === 4 && (
          <section className="flow-section market-results">
            <div className="results-title-row">
              <div>
                <h2>Step 4 — {market}</h2>
                <p>{crop} · {state}</p>
              </div>
              <div className="view-toggle" role="group" aria-label="Forecast display">
                <button type="button" className={view === "chart" ? "active" : ""} onClick={() => setView("chart")}>Chart</button>
                <button type="button" className={view === "table" ? "active" : ""} onClick={() => setView("table")}>Table</button>
              </div>
            </div>

            {loading ? <p className="status-text">Loading historical prices and forecast…</p> : error ? null : forecast && (
              <>
                <div className="signal-summary">
                  <span className={`signal-badge ${signal === "SELL" ? "sell" : signal === "HOLD" ? "hold" : "unknown"}`}>{signal || "N/A"}</span>
                  <div><strong>{signal ? `${signal} signal` : "Signal unavailable"}</strong><p>Model-generated signal for this commodity and market.</p></div>
                </div>

                {view === "chart" ? (
                  <div className="chart-card">
                    {chartData.length === 0 ? <p className="status-text">No chart data is available.</p> : (
                      <ResponsiveContainer width="100%" height={420}>
                        <ComposedChart data={chartData} margin={{ top: 16, right: 24, left: 8, bottom: 12 }}>
                          <CartesianGrid strokeDasharray="3 3" vertical={false} />
                          <XAxis dataKey="date" tickFormatter={formatDate} minTickGap={28} />
                          <YAxis tickFormatter={(value) => `₹${Number(value).toLocaleString("en-IN")}`} />
                          <Tooltip
                            labelFormatter={(label) => formatDate(label)}
                            formatter={(value, name) => {
                              if (Array.isArray(value)) return [ `${formatPrice(value[0])} – ${formatPrice(value[1])}`, "Confidence Band" ];
                              return [formatPrice(value), name];
                            }}
                          />
                          <Legend />
                          <Area type="monotone" dataKey="confidence_band" name="Confidence Band"
                            stroke="#8bbd99" fill="#b9ddc2" fillOpacity={0.45} connectNulls={false} />
                          <Line type="monotone" dataKey="price" name="Historical Price"
                            stroke="#8b9290" strokeWidth={2} dot={false} connectNulls={false} />
                          <Line type="monotone" dataKey="predicted_price" name="Forecast"
                            stroke="#23834d" strokeWidth={2.5} strokeDasharray="7 5" dot={{ r: 3 }} connectNulls={false} />
                          {firstForecastDate && <ReferenceLine x={firstForecastDate} stroke="#6b8272" strokeDasharray="4 4" />}
                        </ComposedChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                ) : (
                  <div className="table-card">
                    <div className="table-scroll">
                      <table>
                        <thead><tr><th>Forecast Date</th><th>Predicted Price</th><th>Lower Bound</th><th>Upper Bound</th><th>Signal</th></tr></thead>
                        <tbody>
                          {forecastRows.map((row) => (
                            <tr key={row.date}>
                              <td>{formatDate(row.date)}</td>
                              <td>{formatPrice(row.predicted_price)}</td>
                              <td>{formatPrice(row.lower_bound)}</td>
                              <td>{formatPrice(row.upper_bound)}</td>
                              <td><span className={`signal-badge ${signal === "SELL" ? "sell" : signal === "HOLD" ? "hold" : "unknown"}`}>{signal || "N/A"}</span></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </>
            )}
          </section>
        )}

        {error && <div className="error-box" role="alert">{error}</div>}
      </section>

      <footer className="forecast-footer">Historical model output · Data as of 29 Dec 2025</footer>
    </main>
  );
}
