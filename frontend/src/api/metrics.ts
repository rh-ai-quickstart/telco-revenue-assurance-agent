import type { DashboardData, QoeDistributionItem } from "@/data/dashboard";

const REGION_NAMES: Record<number, string> = {
  0: "North",
  1: "East",
  2: "South",
  3: "West",
};

export async function fetchDashboardData(): Promise<DashboardData> {
  const res = await fetch("/api/metrics/dashboard");
  if (!res.ok) throw new Error(`Metrics fetch failed: ${res.status}`);
  const d = await res.json();

  const total: number = d.customers.total;
  const qoeTotal: number =
    d.qoe_distribution.poor + d.qoe_distribution.fair + d.qoe_distribution.good;

  const qoeDistribution: QoeDistributionItem[] = [
    {
      label: "Good",
      count: d.qoe_distribution.good,
      percentage: qoeTotal > 0 ? d.qoe_distribution.good / qoeTotal : 0,
    },
    {
      label: "Fair",
      count: d.qoe_distribution.fair,
      percentage: qoeTotal > 0 ? d.qoe_distribution.fair / qoeTotal : 0,
    },
    {
      label: "Poor",
      count: d.qoe_distribution.poor,
      percentage: qoeTotal > 0 ? d.qoe_distribution.poor / qoeTotal : 0,
    },
  ];

  return {
    generatedAt: new Date().toISOString(),
    metrics: {
      revenueAtRisk: d.customers.revenue_at_risk,
      churnRate: d.customers.churn_rate,
      churnCount: Math.round(total * d.customers.churn_rate),
      fraudRate: d.transactions.fraud_rate,
      fraudCount: d.transactions.fraud_count,
      totalCount: total,
      qoeAverageScore: d.avg_qoe_score,
      qoeDistribution,
    },
    regions: (d.regions as Array<{
      region_id: number;
      customer_count: number;
      churn_count: number;
      churn_rate: number;
      fraud_count: number;
      fraud_rate: number;
      avg_qoe_score: number;
      season: string;
    }>)
      .map((r) => ({
        region: REGION_NAMES[r.region_id] ?? `Region ${r.region_id}`,
        regionId: r.region_id,
        customers: r.customer_count,
        churnCount: r.churn_count ?? Math.round(r.customer_count * r.churn_rate),
        churnRate: r.churn_rate,
        fraudCount: r.fraud_count ?? 0,
        fraudRate: r.fraud_rate ?? 0,
        qoeScore: r.avg_qoe_score,
        season: r.season,
      }))
      .sort((a, b) => b.churnRate - a.churnRate),
  };
}
