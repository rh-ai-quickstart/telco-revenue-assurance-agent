import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  createScoringRun,
  getRunMetrics,
  getScoringRun,
  listScoringRuns,
  type RunMetrics,
  type ScoringRun,
  type ScoringRunAccepted,
  type ScoringRunPage,
} from '../services/api';

const scoringRunKeys = {
  all: ['scoring-runs'] as const,
  list: () => [...scoringRunKeys.all, 'list'] as const,
  detail: (runId: string | null) => [...scoringRunKeys.all, 'detail', runId] as const,
  metrics: (runId: string | null) => ['run-metrics', runId] as const,
};

function isActive(status: string | undefined): boolean {
  return status === 'queued' || status === 'running';
}

export function useScoringRuns(): UseQueryResult<ScoringRunPage, Error> {
  return useQuery({
    queryKey: scoringRunKeys.list(),
    queryFn: () => listScoringRuns(),
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => isActive(run.status)) ? 5_000 : 20_000,
  });
}

export function useScoringRun(runId: string | null): UseQueryResult<ScoringRun, Error> {
  return useQuery({
    queryKey: scoringRunKeys.detail(runId),
    queryFn: () => {
      if (runId === null) {
        throw new Error('Select a scoring run before requesting its status.');
      }
      return getScoringRun(runId);
    },
    enabled: runId !== null,
    refetchInterval: (query) => (isActive(query.state.data?.status) ? 2_500 : false),
  });
}

export function useRunMetrics(runId: string | null): UseQueryResult<RunMetrics, Error> {
  return useQuery({
    queryKey: scoringRunKeys.metrics(runId),
    queryFn: () => {
      if (runId === null) {
        throw new Error('Select a scoring run before requesting its metrics.');
      }
      return getRunMetrics(runId);
    },
    enabled: runId !== null,
    refetchInterval: (query) => (isActive(query.state.data?.status) ? 5_000 : false),
  });
}

export function useCreateScoringRun(): UseMutationResult<ScoringRunAccepted, Error, void, unknown> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createScoringRun,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: scoringRunKeys.all });
    },
  });
}
