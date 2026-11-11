import type { QoeDistributionItem } from "@/data/dashboard";

export function QoeDistributionPanel({ distribution }: { distribution: QoeDistributionItem[] }) {
  return (
    <section className="panel qoe-panel">
      <div className="panel-heading">
        <h2>Quality of experience</h2>
        <span className="panel-badge">Customer sessions</span>
      </div>
      <div className="distribution">
        {distribution.map((item) => (
          <div className="distribution-row" data-quality={item.label} key={item.label}>
            <div className="distribution-meta">
              <span>{item.label}</span>
              <span>
                {item.count.toLocaleString()} · {(item.percentage * 100).toFixed(1)}%
              </span>
            </div>
            <div className="bar" role="presentation">
              <span style={{ width: `${item.percentage * 100}%` }} />
            </div>
          </div>
        ))}
      </div>
      <p className="panel-note">
        Quality is grouped from service latency, packet loss, and connection stability signals.
      </p>
    </section>
  );
}
