import type { RegionalRisk } from "@/data/dashboard";

function QoeBadge({ score }: { score: number }) {
  const cls =
    score >= 80
      ? "qoe-badge qoe-badge--good"
      : score >= 65
        ? "qoe-badge qoe-badge--fair"
        : "qoe-badge qoe-badge--poor";
  const label = score >= 80 ? "Good" : score >= 65 ? "Fair" : "Poor";
  return <span className={cls}>{label}</span>;
}

export function WorstRegionCard({ regions }: { regions: RegionalRisk[] }) {
  const worst = regions.at(0); // already sorted by churnRate desc

  if (!worst) {
    return (
      <article className="metric-card worst-region-card worst-region-card--loading" aria-busy="true" />
    );
  }

  return (
    <article className="metric-card worst-region-card" aria-label="Highest churn region">
      <span className="metric-label">Highest churn region</span>
      <strong className="worst-region-name">{worst.region}</strong>

      <dl className="worst-region-stats">
        <div className="worst-region-stat">
          <dt>Churn rate</dt>
          <dd className="worst-region-churn">{(worst.churnRate * 100).toFixed(1)}%</dd>
        </div>
        <div className="worst-region-stat">
          <dt>Customers at risk</dt>
          <dd>{worst.churnCount.toLocaleString()}</dd>
        </div>
        <div className="worst-region-stat">
          <dt>Avg QoE</dt>
          <dd>
            {worst.qoeScore.toFixed(1)}&nbsp;
            <QoeBadge score={worst.qoeScore} />
          </dd>
        </div>
        <div className="worst-region-stat">
          <dt>Season</dt>
          <dd>{worst.season}</dd>
        </div>
      </dl>
    </article>
  );
}
