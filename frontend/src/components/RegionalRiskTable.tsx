import { useState } from "react";
import type { RegionalRisk } from "@/data/dashboard";

type SortKey = "churn" | "fraud" | "qoe";

const SORT_LABELS: Record<SortKey, string> = {
  churn: "Churn",
  fraud: "Fraud",
  qoe: "QoE",
};

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function sortRegions(regions: RegionalRisk[], key: SortKey): RegionalRisk[] {
  return [...regions].sort((a, b) => {
    if (key === "churn") return b.churnRate - a.churnRate;
    if (key === "fraud") return b.fraudRate - a.fraudRate;
    return a.qoeScore - b.qoeScore; // low QoE = worst → sort ascending
  });
}

export function RegionalRiskTable({ regions }: { regions: RegionalRisk[] }) {
  const [sortKey, setSortKey] = useState<SortKey>("churn");

  const sorted = sortRegions(regions, sortKey);

  return (
    <section className="panel table-panel">
      <div className="panel-heading">
        <h2>Regional risk summary</h2>
        <div className="sort-controls" role="group" aria-label="Sort regions by">
          {(["churn", "fraud", "qoe"] as SortKey[]).map((key) => (
            <button
              key={key}
              className={`sort-btn${sortKey === key ? " sort-btn--active" : ""}`}
              onClick={() => setSortKey(key)}
              aria-pressed={sortKey === key}
            >
              {SORT_LABELS[key]}
            </button>
          ))}
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Region</th>
              <th scope="col">Customers</th>
              <th scope="col" aria-sort={sortKey === "churn" ? "descending" : "none"}>
                Churn rate {sortKey === "churn" && "▼"}
              </th>
              <th scope="col">At risk</th>
              <th scope="col" aria-sort={sortKey === "fraud" ? "descending" : "none"}>
                Fraud rate {sortKey === "fraud" && "▼"}
              </th>
              <th scope="col">Fraud cases</th>
              <th scope="col" aria-sort={sortKey === "qoe" ? "ascending" : "none"}>
                QoE score {sortKey === "qoe" && "▲"}
              </th>
              <th scope="col">Season</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((region) => (
              <tr key={region.region}>
                <th scope="row">{region.region}</th>
                <td>{region.customers.toLocaleString()}</td>
                <td className={sortKey === "churn" ? "col-sorted" : ""}>
                  {formatPercent(region.churnRate)}
                </td>
                <td>{region.churnCount.toLocaleString()}</td>
                <td className={sortKey === "fraud" ? "col-sorted" : ""}>
                  {formatPercent(region.fraudRate)}
                </td>
                <td>{region.fraudCount.toLocaleString()}</td>
                <td className={sortKey === "qoe" ? "col-sorted" : ""}>
                  {region.qoeScore.toFixed(1)} / 100
                </td>
                <td>{region.season}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
