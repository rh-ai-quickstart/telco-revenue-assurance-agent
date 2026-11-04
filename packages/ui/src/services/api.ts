import { z } from 'zod';
import type { components, paths } from '../api/types';

export type ScoringRun = components['schemas']['ScoringRun'];
export type ScoringRunPage =
  paths['/scoring-runs']['get']['responses'][200]['content']['application/json'];
export type ScoringRunAccepted =
  paths['/scoring-runs']['post']['responses'][202]['content']['application/json'];
export type RunMetrics = paths['/metrics']['get']['responses'][200]['content']['application/json'];
export type ChatMessageRequest = components['schemas']['ChatMessageRequest'];
export type ChatMessageResponse =
  paths['/chat/sessions/{session_id}/messages']['post']['responses'][200]['content']['application/json'];
type ErrorEnvelope = components['schemas']['ErrorEnvelope'];

const dateTimeSchema = z.string().datetime({ offset: true });

const modelProgressSchema: z.ZodType<components['schemas']['ModelProgress']> = z.object({
  status: z.enum(['queued', 'running', 'completed', 'failed']),
  processed_rows: z.number().int().nonnegative(),
  total_rows: z.number().int().nonnegative(),
});

const scoringRunSchema: z.ZodType<ScoringRun> = z.object({
  run_id: z.string().uuid(),
  status: z.enum(['queued', 'running', 'completed', 'failed']),
  created_at: dateTimeSchema,
  started_at: dateTimeSchema.nullable(),
  completed_at: dateTimeSchema.nullable(),
  progress: z.object({
    churn: modelProgressSchema,
    fraud: modelProgressSchema,
    qoe: modelProgressSchema,
  }),
  error_code: z.string().nullable(),
});

const scoringRunPageSchema: z.ZodType<ScoringRunPage> = z.object({
  items: z.array(scoringRunSchema),
  next_cursor: z.string().nullable(),
});

const scoringRunAcceptedSchema: z.ZodType<ScoringRunAccepted> = z.object({
  run_id: z.string().uuid(),
  status: z.literal('queued'),
  created_at: dateTimeSchema,
});

const classAggregateSchema: z.ZodType<components['schemas']['ClassAggregate']> = z.object({
  class_id: z.number().int(),
  prediction_count: z.number().int().nonnegative(),
  dollar_weight_sum: z.number().nonnegative().nullable(),
});

const runMetricsSchema: z.ZodType<RunMetrics> = z.object({
  run_id: z.string().uuid(),
  status: z.enum(['queued', 'running', 'completed', 'failed']),
  as_of: dateTimeSchema,
  churn: z.object({
    customer_count: z.number().int().nonnegative(),
    classes: z.array(classAggregateSchema),
  }),
  fraud: z.object({
    transaction_count: z.number().int().nonnegative(),
    fraud_count: z.number().int().nonnegative(),
    classes: z.array(classAggregateSchema),
  }),
  qoe: z.object({
    customer_count: z.number().int().nonnegative(),
    mean_score: z.number().min(0).max(100).nullable(),
    minimum_score: z.number().min(0).max(100).nullable(),
    maximum_score: z.number().min(0).max(100).nullable(),
  }),
});

const chatMessageResponseSchema: z.ZodType<ChatMessageResponse> = z.object({
  session_id: z.string().uuid(),
  request_id: z.string().uuid(),
  response_id: z.string(),
  assistant_message: z.string(),
  created_at: dateTimeSchema,
});

const errorEnvelopeSchema: z.ZodType<ErrorEnvelope> = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    request_id: z.string(),
    retryable: z.boolean(),
    details: z.array(
      z.object({
        field: z.string().nullable(),
        reason: z.string(),
        limit: z.number().int().nullable(),
        current_run_id: z.string().uuid().nullable(),
      }),
    ),
  }),
});

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly retryable: boolean;
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    error: ErrorEnvelope['error'],
    retryAfterSeconds: number | null = null,
  ) {
    super(error.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = error.code;
    this.requestId = error.request_id;
    this.retryable = error.retryable;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

const maximumRetries = 2;
const maximumRetryDelayMs = 8_000;

function apiBaseUrl(): string {
  const configuredBase = import.meta.env.VITE_API_BASE_URL?.trim();
  return (configuredBase || '/api/v1').replace(/\/+$/, '');
}

function retryDelayMs(error: ApiError, retryIndex: number): number {
  if (error.retryAfterSeconds !== null) {
    return Math.min(maximumRetryDelayMs, Math.max(1_000, error.retryAfterSeconds * 1_000));
  }
  return Math.min(maximumRetryDelayMs, 1_000 * 2 ** retryIndex);
}

function pause(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function toApiError(response: Response): Promise<ApiError> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  const retryHeader = response.headers.get('Retry-After');
  const parsedRetryAfter = retryHeader === null ? Number.NaN : Number(retryHeader);
  const retryAfterSeconds =
    Number.isInteger(parsedRetryAfter) && parsedRetryAfter > 0 ? parsedRetryAfter : null;
  const parsed = errorEnvelopeSchema.safeParse(payload);
  if (parsed.success) {
    return new ApiError(response.status, parsed.data.error, retryAfterSeconds);
  }

  const fallback: ErrorEnvelope['error'] = {
    code: 'HTTP_ERROR',
    message: `The API request failed with status ${response.status}.`,
    request_id: '',
    retryable: response.status === 503,
    details: [],
  };
  return new ApiError(response.status, fallback, retryAfterSeconds);
}

async function requestJson<T>(path: string, init: RequestInit, schema: z.ZodType<T>): Promise<T> {
  let retryIndex = 0;
  while (true) {
    let response: Response;
    try {
      const headers = new Headers(init.headers);
      headers.set('Accept', 'application/json');
      response = await fetch(`${apiBaseUrl()}${path}`, {
        ...init,
        headers,
        credentials: 'same-origin',
      });
    } catch {
      throw new ApiError(0, {
        code: 'NETWORK_ERROR',
        message: 'The API could not be reached. Check the connection and try again.',
        request_id: '',
        retryable: true,
        details: [],
      });
    }

    if (!response.ok) {
      const error = await toApiError(response);
      if (response.status !== 503 || retryIndex >= maximumRetries) {
        throw error;
      }
      await pause(retryDelayMs(error, retryIndex));
      retryIndex += 1;
      continue;
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ApiError(response.status, {
        code: 'INVALID_RESPONSE',
        message: 'The API returned an unreadable response.',
        request_id: '',
        retryable: false,
        details: [],
      });
    }

    const parsed = schema.safeParse(payload);
    if (!parsed.success) {
      throw new ApiError(response.status, {
        code: 'INVALID_RESPONSE',
        message: 'The API returned a response that did not match its contract.',
        request_id: '',
        retryable: false,
        details: [],
      });
    }
    return parsed.data;
  }
}

export function listScoringRuns(limit = 20): Promise<ScoringRunPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  return requestJson(`/scoring-runs?${params.toString()}`, { method: 'GET' }, scoringRunPageSchema);
}

export function createScoringRun(): Promise<ScoringRunAccepted> {
  return requestJson(
    '/scoring-runs',
    {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    },
    scoringRunAcceptedSchema,
  );
}

export function getScoringRun(runId: string): Promise<ScoringRun> {
  return requestJson(
    `/scoring-runs/${encodeURIComponent(runId)}`,
    { method: 'GET' },
    scoringRunSchema,
  );
}

export function getRunMetrics(runId: string): Promise<RunMetrics> {
  const params = new URLSearchParams({ run_id: runId });
  return requestJson(`/metrics?${params.toString()}`, { method: 'GET' }, runMetricsSchema);
}

export function sendChatMessage(
  sessionId: string,
  request: ChatMessageRequest,
): Promise<ChatMessageResponse> {
  return requestJson(
    `/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
    chatMessageResponseSchema,
  );
}
