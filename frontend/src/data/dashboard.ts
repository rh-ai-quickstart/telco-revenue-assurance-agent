export interface QoeDistributionItem {
  label: "Poor" | "Fair" | "Good";
  count: number;
  percentage: number;
}

export interface DashboardMetrics {
  revenueAtRisk: number;
  churnRate: number;
  churnCount: number;
  fraudRate: number;
  fraudCount: number;
  totalCount: number;
  qoeAverageScore: number;
  qoeDistribution: QoeDistributionItem[];
}

export interface RegionalRisk {
  region: string;
  regionId: number;
  customers: number;
  churnCount: number;
  churnRate: number;
  fraudCount: number;
  fraudRate: number;
  qoeScore: number;
  season: string;
}

export interface DashboardData {
  generatedAt: string;
  metrics: DashboardMetrics;
  regions: RegionalRisk[];
}
