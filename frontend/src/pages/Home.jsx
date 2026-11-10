import { Link } from "react-router-dom";

const features = [
  {
    icon: "▥",
    title: "Historical Analysis",
    description: "1.18 million price records across 3 states"
  },
  {
    icon: "⌁",
    title: "Prophet Forecasting",
    description: "7-day ahead price predictions per mandi"
  },
  {
    icon: "✓",
    title: "Sell or Hold Signal",
    description: "Clear recommendation for each market"
  }
];

const steps = [
  ["01", "Select your state"],
  ["02", "Choose a crop"],
  ["03", "Pick your mandi — see the forecast"]
];

export default function Home() {
  return (
    <main className="home-page">
      <section className="home-hero">
        <header className="home-header">
          <Link className="brand" to="/">Live Mandi Price Advisor</Link>
          <span className="header-note">Historical demonstration</span>
        </header>
        <div className="hero-content">
          <p className="eyebrow">AGRICULTURAL MARKET INTELLIGENCE</p>
          <h1>Know When to Sell.<br />Know Where to Sell.</h1>
          <p className="hero-subtitle">
            AI-powered mandi price forecasts for farmers and traders across India.
          </p>
          <Link className="primary-button" to="/forecast">Let’s Predict <span aria-hidden="true">→</span></Link>
        </div>
        <a className="scroll-cue" href="#what-it-does">Discover how it works <span>↓</span></a>
      </section>

      <section className="home-section feature-section" id="what-it-does">
        <div className="section-intro">
          <p className="eyebrow">WHAT IT DOES</p>
          <h2>Market data, made meaningful.</h2>
          <p>Explore historical mandi prices and model-generated forecasts in one place.</p>
        </div>
        <div className="feature-grid">
          {features.map((feature) => (
            <article className="feature-card" key={feature.title}>
              <span className="feature-icon" aria-hidden="true">{feature.icon}</span>
              <h3>{feature.title}</h3>
              <p>{feature.description}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="home-section process-section">
        <div className="section-intro">
          <p className="eyebrow">HOW IT WORKS</p>
          <h2>Three steps to your market outlook.</h2>
        </div>
        <div className="steps-grid">
          {steps.map(([number, label]) => (
            <article className="step-card" key={number}>
              <span className="step-number">{number}</span>
              <h3>{label}</h3>
            </article>
          ))}
        </div>
      </section>

      <section className="impact-section">
        <div>
          <p className="eyebrow">REAL-WORLD IMPACT</p>
          <h2>Make decisions with a clearer picture.</h2>
          <p>
            Uncertain mandi prices can make it difficult for farmers to decide when and where to sell.
            This demonstration brings historical price trends, short-horizon model forecasts and
            simple Sell or Hold signals together to support more informed market decisions.
          </p>
          <Link className="primary-button" to="/forecast">Let’s Predict <span aria-hidden="true">→</span></Link>
        </div>
      </section>

      <footer className="home-footer">
        <div className="footer-meta">
          <p>Data as of 29 Dec 2025 · Historical demonstration · Not live market data</p>
        </div>
        <div className="team-section">
          <h3>Project Team</h3>
          <div className="team-list">
            <span>Tanmoy Maiti</span>
            <span>Raja Kumar</span>
            <span>Arnab Das</span>
            <span>Aditya Bharati</span>
          </div>
        </div>
        <p className="copyright">Live Mandi Price Advisor · Academic project demonstration</p>
      </footer>
    </main>
  );
}
