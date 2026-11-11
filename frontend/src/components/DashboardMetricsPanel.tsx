import type { DashboardMetrics } from "@/data/dashboard";

function formatCompactCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function DashboardMetricsPanel({ metrics }: { metrics: DashboardMetrics | null }) {
  if (!metrics) {
    return (
      <section className="metric-grid" aria-label="Key revenue assurance metrics" aria-busy="true">
        {Array.from({ length: 4 }).map((_, i) => (
          <article className="metric-card metric-card--loading" key={i} />
        ))}
      </section>
    );
  }

  return (
    <section className="metric-grid" aria-label="Key revenue assurance metrics">
      <article className="metric-card">
        <span className="metric-label">Revenue at risk</span>
        <strong>{formatCompactCurrency(metrics.revenueAtRisk)}</strong>
        <p>Active customer billing exposed to churn</p>
      </article>
      <article className="metric-card">
        <span className="metric-label">Churn rate</span>
        <strong>{(metrics.churnRate * 100).toFixed(1)}%</strong>
        <p>
          {metrics.churnCount.toLocaleString()} of {metrics.totalCount.toLocaleString()} customers
        </p>
      </article>
      <article className="metric-card">
        <span className="metric-label">Fraud cases</span>
        <strong>{metrics.fraudCount.toLocaleString()}</strong>
        <p>{(metrics.fraudRate * 100).toFixed(1)}% of transactions flagged</p>
      </article>
      <article className="metric-card">
        <span className="metric-label">Avg QoE score</span>
        <strong>{metrics.qoeAverageScore.toFixed(1)} / 100</strong>
        <p>Average quality-of-experience rating</p>
      </article>
    </section>
  );
}
