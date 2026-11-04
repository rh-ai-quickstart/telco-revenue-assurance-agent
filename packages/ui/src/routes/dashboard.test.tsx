/* @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as React from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { components } from '../api/types';
import { Dashboard } from './dashboard';

const runId = '5f70f438-60ba-46f8-80f7-88c18d1707ac';
const sessionId = '8239f3a1-94f0-4e9a-9179-2a7b10ef45b8';
const run = {
  run_id: runId,
  status: 'completed',
  created_at: '2026-10-21T11:00:00Z',
  started_at: '2026-10-21T11:00:02Z',
  completed_at: '2026-10-21T11:03:30Z',
  progress: {
    churn: { status: 'completed', processed_rows: 1250, total_rows: 1250 },
    fraud: { status: 'completed', processed_rows: 6840, total_rows: 6840 },
    qoe: { status: 'completed', processed_rows: 1125, total_rows: 1125 },
  },
  error_code: null,
} satisfies components['schemas']['ScoringRun'];

const metrics = {
  run_id: runId,
  status: 'completed',
  as_of: '2026-10-21T11:03:30Z',
  churn: {
    customer_count: 1250,
    classes: [
      { class_id: 0, prediction_count: 900, dollar_weight_sum: 210000 },
      { class_id: 1, prediction_count: 350, dollar_weight_sum: 94500 },
    ],
  },
  fraud: {
    transaction_count: 6840,
    fraud_count: 17,
    classes: [
      { class_id: 0, prediction_count: 6823, dollar_weight_sum: null },
      { class_id: 1, prediction_count: 17, dollar_weight_sum: null },
    ],
  },
  qoe: {
    customer_count: 1125,
    mean_score: 78.4,
    minimum_score: 33.2,
    maximum_score: 99.1,
  },
} satisfies components['schemas']['RunMetrics'];

const chatResponse = {
  session_id: sessionId,
  request_id: 'f28fb20a-03b3-4d71-8a5a-6a29ab5cf93c',
  response_id: 'resp_01J8Z2N5M9K7',
  assistant_message: 'Class 1 contains the greatest customer dollar weight in this run.',
  created_at: '2026-10-21T11:10:00Z',
} satisfies components['schemas']['ChatMessageResponse'];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolvePromise: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve(value: T): void {
      resolvePromise?.(value);
    },
  };
}

function installApiMock(
  chatHandler?: () => Response | Promise<Response>,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const requestUrl = new URL(String(input), window.location.origin);
      if (requestUrl.pathname === '/api/v1/scoring-runs' && init?.method !== 'POST') {
        return jsonResponse({ items: [run], next_cursor: null });
      }
      if (requestUrl.pathname === '/api/v1/scoring-runs' && init?.method === 'POST') {
        return jsonResponse({ run_id: runId, status: 'queued', created_at: run.created_at }, 202);
      }
      if (requestUrl.pathname === `/api/v1/scoring-runs/${runId}`) {
        return jsonResponse(run);
      }
      if (requestUrl.pathname === '/api/v1/metrics') {
        return jsonResponse(metrics);
      }
      if (requestUrl.pathname.startsWith('/api/v1/chat/sessions/')) {
        return chatHandler ? await chatHandler() : jsonResponse(chatResponse);
      }
      throw new Error(`Unexpected API request: ${requestUrl.pathname}`);
    },
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderDashboard(): void {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  render(
    <React.Fragment>
      <QueryClientProvider client={queryClient}>
        <Dashboard />
      </QueryClientProvider>
    </React.Fragment>,
  );
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('revenue assurance dashboard', () => {
  it('renders metrics for the selected scoring run', async () => {
    const fetchMock = installApiMock();
    renderDashboard();

    const runSelector = await screen.findByRole('combobox', { name: 'Select scoring run' });
    await waitFor(() => expect((runSelector as HTMLSelectElement).value).toBe(runId));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([input]) =>
            new URL(String(input), window.location.origin).pathname === '/api/v1/metrics',
        ),
      ).toBe(true);
    });

    const churnCard = await screen.findByRole('region', { name: 'Churn propensity metrics' });
    const fraudCard = screen.getByRole('region', { name: 'Transaction fraud metrics' });
    const qoeCard = screen.getByRole('region', { name: 'Service quality metrics' });
    await within(churnCard).findByText('1,250');
    expect(within(churnCard).getByText('1,250')).toBeTruthy();
    expect(within(churnCard).getByText(/94,500 dollar weight/)).toBeTruthy();
    expect(within(fraudCard).getByText('17')).toBeTruthy();
    expect(within(qoeCard).getByText('78.4')).toBeTruthy();
    expect(within(qoeCard).getByText('1,125')).toBeTruthy();
  });

  it('shows a pending reply and then the completed REST response', async () => {
    const chatRequest = deferred<Response>();
    installApiMock(() => chatRequest.promise);
    renderDashboard();

    const input = await screen.findByRole('textbox', { name: 'Ask the revenue assurance agent' });
    fireEvent.change(input, {
      target: { value: 'Which class has the greatest customer dollar weight?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send question' }));

    expect(await screen.findByRole('status', { name: 'Assistant response pending' })).toBeTruthy();
    chatRequest.resolve(jsonResponse(chatResponse));
    expect(await screen.findByText(chatResponse.assistant_message)).toBeTruthy();
  });

  it('replays a transient chat failure with the same request ID and message', async () => {
    let attempts = 0;
    const fetchMock = installApiMock(() => {
      attempts += 1;
      if (attempts === 1) {
        return new Response(
          JSON.stringify({
            error: {
              code: 'UPSTREAM_UNAVAILABLE',
              message: 'The agent response is temporarily unavailable.',
              request_id: 'req_01J8Z2N5M9KA',
              retryable: true,
              details: [],
            },
          }),
          {
            status: 503,
            headers: { 'Content-Type': 'application/json', 'Retry-After': '1' },
          },
        );
      }
      return jsonResponse(chatResponse);
    });
    renderDashboard();

    const input = await screen.findByRole('textbox', { name: 'Ask the revenue assurance agent' });
    fireEvent.change(input, { target: { value: 'Summarize the selected run.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send question' }));

    expect(await screen.findByRole('status', { name: 'Assistant response pending' })).toBeTruthy();
    expect(
      await screen.findByText(chatResponse.assistant_message, {}, { timeout: 4_000 }),
    ).toBeTruthy();
    const chatCalls = fetchMock.mock.calls.filter(([request]) =>
      String(request).includes('/chat/sessions/'),
    );
    expect(attempts).toBe(2);
    expect(chatCalls).toHaveLength(2);
    expect(chatCalls[1]?.[1]?.body).toBe(chatCalls[0]?.[1]?.body);
  });

  it('shows a contract error from chat without retrying a 4xx response', async () => {
    const fetchMock = installApiMock(() =>
      jsonResponse(
        {
          error: {
            code: 'IDEMPOTENCY_CONFLICT',
            message: 'request_id is already used for a different message.',
            request_id: 'req_01J8Z2N5M9K9',
            retryable: false,
            details: [],
          },
        },
        409,
      ),
    );
    renderDashboard();

    const input = await screen.findByRole('textbox', { name: 'Ask the revenue assurance agent' });
    fireEvent.change(input, { target: { value: 'Summarize the selected run.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send question' }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('IDEMPOTENCY_CONFLICT');
    expect(alert.textContent).toContain('request_id is already used for a different message.');
    const chatCalls = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes('/chat/sessions/'),
    );
    expect(chatCalls).toHaveLength(1);
  });
});
