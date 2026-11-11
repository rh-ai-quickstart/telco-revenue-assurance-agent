import { useEffect, useState } from "react";
import { fetchDashboardData } from "@/api/metrics";
import type { DashboardData } from "@/data/dashboard";
import { DashboardMetricsPanel } from "@/components/DashboardMetricsPanel";
import { QoeDistributionPanel } from "@/components/QoeDistributionPanel";
import { RegionalRiskTable } from "@/components/RegionalRiskTable";
import { WorstRegionCard } from "@/components/WorstRegionCard";
import { FloatingChatbot } from "@/components/FloatingChatbot";

export function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchDashboardData()
      .then(setData)
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Failed to load metrics"),
      );
  }, []);

  return (
    <main className="dashboard-shell">
      <header className="dashboard-header">
        <div>
          <h1>Telco revenue assurance</h1>
          <p className="dashboard-subtitle">
            Monitor churn, fraud exposure, and service quality in one operational view.
          </p>
        </div>
        {data && (
          <div className="dataset-card">
            <span>Live data</span>
            <time dateTime={data.generatedAt}>
              Updated {new Date(data.generatedAt).toLocaleTimeString()}
            </time>
          </div>
        )}
      </header>

      {error && (
        <div className="dashboard-error" role="alert">
          Unable to load metrics: {error}
        </div>
      )}

      <div className="dashboard-body">
        <div className="dashboard-primary">
          <RegionalRiskTable regions={data?.regions ?? []} />
        </div>

        <aside className="dashboard-rail" aria-label="Key metrics and service quality">
          <DashboardMetricsPanel metrics={data?.metrics ?? null} />
          <QoeDistributionPanel distribution={data?.metrics?.qoeDistribution ?? []} />
          <WorstRegionCard regions={data?.regions ?? []} />
        </aside>
      </div>

      <FloatingChatbot />
    </main>
  );
}
