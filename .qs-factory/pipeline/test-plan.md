# Telco revenue assurance deployment test plan

Prepared: 2026-10-25T12:33:17Z. Namespace/release: `telco-revenue-assurance-agent`.
Canonical plan: `.qs-factory/pipeline/test-plan.md`. This copy is also retained in the debug state directory.

Status: **PLANNED; no tests executed during preparation**. Image reconciliation is in progress. Obtain the mandatory fresh namespace scan before live execution. Source data is not seeded; real model artifacts, feature PVC contents, model authorization, and a usable external LLM credential are pending. Application journeys below are **BLOCKED**, rather than observed failures. The last token verification (2026-10-25T12:17:50Z) reported an empty `telco-remote-llm/REMOTE_LLM_API_KEY`; never include its value in evidence.

The preceding frontend probes, completed database hooks, and 58 database checks establish limited foundation evidence. They do not establish scoring, model inference, OAuth browser access, or chat success. Prior local test counts are historical evidence, not results from this plan.

## Sources and result rules

Derived from `Makefile`, `.qs-factory/designs/architecture-spec.yaml`, `contracts/openapi.yaml`, `contracts/data-fetch-mcp.json`, `contracts/prediction-mcp.json`, the three `contracts/*-inference.json` files, API settings/routes/services, UI dashboard/ChatPanel/API service, and chart templates. The selected deployment remains authoritative only in the debug `deploy-analysis.yaml`; do not change it while executing this plan. Use the external LLM variant already selected there. No local Llama/Hugging Face deployment is part of this test plan.

Use stable IDs below for all later result records. **PASS** requires the actual expected response and assertions; **FAIL** requires an observed violation after prerequisites were met; **BLOCKED** means a prerequisite is absent; **NOT_RUN** means execution has not occurred. If a prerequisite test fails or is blocked, continue independent tests and record dependent tests as `skipped` with an explicit `BLOCKED: <prerequisite and cause>` reason in the skill's results schema. A timeout with an active operation is pending/inconclusive, not evidence of a completed successful journey. Preserve request/run IDs before retrying writes.

Record UTC start/end, source revision/diff fingerprint, deployed image/digest, command or browser action, expected/actual status and safe response fields. Do not capture credentials, cookies, Authorization headers, full environment dumps, or unrestricted HAR files. Inspect only Secret object/key metadata and sanitized credential usability booleans.

## Prerequisites and bounded preflight

Do not execute accepted scoring/chat writes until the coordinator has confirmed all real application dependencies ready. No training, fabricated rows, placeholder model manifests, dummy feature tensors, empty artifact mounts, OAuth bypass, deployment mutations, seed creation, hook recreation, or destructive restart belongs to this test plan.

1. The post-image namespace scan is complete. Frontend, API, data-fetch, prediction-tools, OGX and PostgreSQL have their intended images and ready endpoints. Application images use `quay.io/yelias`; vendor images retain their approved vendor registries. Preserve all existing debug attempt history.
2. Current role-init, migration and grants Jobs are Complete with no current failure condition. Expected latest UIDs from existing evidence are `ac442b24-f6f5-4d8a-884b-3dafd39371a3`, `3ede1340-7bd0-4e03-b709-aa56a1d4b49b`, and `d897cbf2-6714-4658-a927-baefd405fd1c`. Four orphan Failed Pods from deleted migration UID `9549f149-02b7-46a4-9dad-307c952ddf38` are historical; retain their actual phase without attributing them to the completed replacement Job.
3. The seven application tables exist, PostgreSQL vector extension and frozen roles/grants are verified, and exactly one discovered `app`/`public` Alembic version table reports `0001_initial_schema`. Reuse the reviewed read-only checks in `additional-internal-recovery-plan.yaml`; do not rerun bootstrap or migrations as a test.
4. Real first-install seed completion/provenance is recorded, and `app.customers`, `app.billing_cycles`, `app.transactions`, `app.qoe_samples` are each nonempty. API's init container requires this. Preserve matching active/completed seed Jobs and durable seed evidence; a missing hook object alone does not authorize rerunning it. Seeding remains a separately guarded recovery operation, not a test fixture.
5. All three real inference services are Ready, their storage/auth references work, feature PVC is Bound with verified matching `churn.json`, `fraud.json`, `qoe.json`, `scaler.joblib`, and `label_encoders.joblib`, and manifests supply real versions, feature order, tensor names and dtypes. Metadata must match the artifacts; readiness alone is insufficient.
6. OGX is Ready with its durable PostgreSQL state and exactly the four intended MCP tools: `fetch_customer_data`, `predict_churn`, `predict_fraud`, `predict_qoe`. The actual selected external LLM has a nonempty credential and an authenticated successful request, recorded without revealing the credential. Secret/key existence alone does not satisfy this gate.
7. An authorized browser session can complete the real OpenShift OAuth login. If an interactive login is needed, leave authenticated tests BLOCKED until that session exists; do not forge identity headers or bypass the proxy.

All commands below are **planned**, not executed. Run each observation with a finite timeout; do not wait/watch indefinitely. Keep individual observations under 60 seconds. A long scoring run may span observations, but must retain the same run ID and a finite overall deadline agreed from actual seeded volume and serving capacity before submission. If that deadline is reached, inspect/record the existing run rather than starting a replacement.

```sh
# P02: read-only release evidence. Exit zero does not mean the release status is deployed.
make verify-deploy NAMESPACE=telco-revenue-assurance-agent HELM='helm -n telco-revenue-assurance-agent'

# P02: obtain actual resource readiness and admitted frontend route; never invent a route host.
oc get deployment,statefulset,pod,pvc,job,inferenceservice,servingruntime -n telco-revenue-assurance-agent --request-timeout=10s
oc get route -n telco-revenue-assurance-agent --request-timeout=10s -o custom-columns=NAME:.metadata.name,SERVICE:.spec.to.name,HOST:.spec.host,ADMITTED:.status.ingress[*].conditions[*].status

# P03: read-only source-data gate; no password is printed or passed on a command line.
oc exec pgvector-0 -n telco-revenue-assurance-agent --request-timeout=15s -- psql --no-psqlrc --no-password -U postgres -d telco_revenue_assurance -v ON_ERROR_STOP=1 -Atc "SET statement_timeout='10s'; SET default_transaction_read_only=on; SELECT EXISTS(SELECT 1 FROM app.customers), EXISTS(SELECT 1 FROM app.billing_cycles), EXISTS(SELECT 1 FROM app.transactions), EXISTS(SELECT 1 FROM app.qoe_samples);"

# P04: API's actual probe endpoint. This internal read is not authenticated browser E2E.
oc exec deployment/api -c api -n telco-revenue-assurance-agent --request-timeout=15s -- python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=10); print(r.status)"
```

## Complete test inventory

| ID | Test and exact action | Expected result | Dependencies / preparation state |
| --- | --- | --- | --- |
| P01 | Local source checks: from repo root, run the explicit Python/UI commands below (the underlying `make test` suites). Run chart lint/render with the explicit namespace override below. | Python package and UI suites pass; chart lint/render succeed. Unit/mocked UI results remain separate from live E2E. | Local dependencies installed; NOT_RUN. |
| P02 | Run release/resource/route observations above and consume the mandatory health snapshot, including InferenceServices and ServingRuntimes. Compare intended images/digests. | Factual readiness for every dependency; report failed Helm release independently from staged resource health. | Post-image scan; NOT_RUN. |
| P03 | Verify current completed DB hooks, schema/version/grants and source-data query above using reviewed read-only recovery checks. Verify recorded real seed provenance. | Required tables/version/grants valid; four source booleans true; existing seed completion evidence preserved. | DB hook evidence exists; seed missing, BLOCKED for full gate. |
| P04 | Run API `/openapi.json` read above and inspect paths/schema. | HTTP 200, actual scoring/metrics/chat contract available; do not invent `/health`. | API init and P03; BLOCKED. |
| B01 | Open the discovered HTTPS frontend route in a fresh unauthenticated browser context; then complete normal authorized OAuth login. | Unauthenticated access follows login flow, authorized user reaches dashboard, TLS valid; no proxy startup/error page or broken assets. | Ready frontend and authorized interactive session; NOT_RUN. |
| B02 | In authenticated browser load dashboard, inspect console/network, read `/api/v1/scoring-runs?limit=20`, select an existing run if available. | API returns JSON 200 with `items`/`next_cursor`; UI reflects real data or honest empty state, without canned metrics or failed API/asset requests. | B01, P04; BLOCKED. |
| M01 | From live API container, use its existing settings and MCP client to list the actual tools and fetch a real customer and transaction using the read-only probe below. Follow a real `next_cursor` if returned. | Only intended tools registered; typed `records`/`next_cursor`, approved fields, stable IDs, deterministic pages without duplicate records. | Real seed, API/data-fetch/OGX ready; BLOCKED. |
| M02 | For each of churn/fraud/QoE, use an actual valid seeded subject ID and the existing MCP prediction client, one family per invocation as described below. Correlate the real KServe request and artifact version. | All three model-specific output contracts hold, real model versions supplied, no inference errors or fabricated output; source-to-feature-to-inference path works. | M01, all real models/feature files/storage/auth ready; BLOCKED. |
| S01 | Submit one authenticated scoring POST with no body and a recorded UUID `Idempotency-Key`; replay exactly that key immediately using the browser helper below. | Both responses 202 refer to the same `run_id`; one durable `app.scoring_runs` operation, no duplicate processing. | Complete preflight including real models/seed/auth/token; BLOCKED. |
| S02 | Poll GET `/api/v1/scoring-runs/{run_id}` in bounded observations. While active, attempt one different recorded key only if the run is still observed active. | Progress is nondecreasing and bounded by totals; all three model portions and overall run finish `completed`; a genuinely overlapping request receives 409 `RUN_ALREADY_ACTIVE` without adding a run. If already completed, mark overlap subcheck not exercised; do not start another run to force overlap. | S01; BLOCKED. |
| S03 | Retrieve every result page with the actual opaque cursor, then GET `/api/v1/metrics?run_id={run_id}`; compare to read-only DB aggregates scoped to the same UUID. | No duplicate prediction IDs/subjects per model; expected real source coverage, timestamps/version, classifier/QoE field semantics, totals/class counts/dollar weights/QoE range agree with stored results. | S02 completed; BLOCKED. |
| B03 | After full gate, use dashboard `Run scoring` for a new intentional UI run only if no run is active; record its network UUID/run ID. Observe progress, select runs, refresh page. | UI creates exactly one run, shows its actual progress and completed metrics, selecting another run changes data, refresh reloads persisted runs; any 409/503 is shown honestly. The button is disabled while its create request is pending; no claim that it disables for an entire run. | S03, B02, full gate; BLOCKED. |
| C01 | Through ChatPanel `Send question`, ask the three grounded questions below in the same real browser session; save session/request/response IDs and safe answer excerpts. | 200 contract, nonempty answer grounded in real DB/MCP results, selected external model actually used, no invented customers or class probabilities. | Completed real scoring, OGX/MCP/external LLM all verified; BLOCKED. |
| C02 | Use same-origin chat request below for a recorded message UUID, replay identical session/request/message, then submit changed text with the same IDs. Refresh same browser tab. | Identical replay yields the same `response_id`/answer, one `app.chat_requests` response and no duplicate OGX call; changed content gets 409; session UUID survives refresh in sessionStorage. UI need not persist its full visible transcript. | C01 and full gate; BLOCKED. |
| N01 | In authenticated session GET a well-formed but nonexistent run UUID, and send an invalid read query such as `limit=0`. Use a nonaccepted malformed chat body only after chat gate is ready. | Documented 404/422 error envelopes; no new scoring/chat operation; UI presents useful errors for real failed calls without exposing credentials or SQL internals. Do not break a working dependency to induce a failure. | B02; chat-specific case additionally C01; BLOCKED. |
| D01 | Read-only compare DB persisted run, predictions, metrics, chat request and OGX session correlation after normal API/UI refresh/replay. | Counts and IDs match S01–C02; actual app/OGX data persists through normal requests; no duplicate rows from retry. No forced pod deletion/restart. | Completed scoring/chat; BLOCKED. |

P01 exact commands (Python and UI scripts are the underlying `make test` suites; no tests are run as part of plan preparation):

```sh
uv run --locked --cache-dir /tmp/telco-qs-cache pytest -q packages
pnpm --filter ui test
make helm-lint helm-template NAMESPACE=telco-revenue-assurance-agent HELM='helm -n telco-revenue-assurance-agent'
```

## Live MCP/model method

Use the application's shipped clients inside the API container, with its existing authorized environment; do not print settings or DSNs. A read-only discovery/fetch probe for M01 is:

```sh
oc exec -i deployment/api -c api -n telco-revenue-assurance-agent --request-timeout=50s -- python - <<'PY'
import asyncio
from api.db.session import ApiSettings
from api.services.mcp_clients import McpClients
from data_fetch_tool import FetchCustomerDataInput

async def main():
    client = McpClients.from_settings(ApiSettings())
    for entity in ('customers', 'transactions'):
        result = await client.fetch_customer_data(FetchCustomerDataInput(entity=entity, limit=1))
        assert result.records, f'{entity}: real source data absent'
        print(entity, result.model_dump(mode='json'))
asyncio.run(asyncio.wait_for(main(), timeout=40))
PY
```

List real tools through the same installed FastMCP SDK as the API, separately from fetch/model execution:

```sh
oc exec -i deployment/api -c api -n telco-revenue-assurance-agent --request-timeout=50s -- python - <<'PY'
import asyncio
from fastmcp import Client
from api.db.session import ApiSettings

async def main():
    s = ApiSettings()
    for endpoint, expected in ((s.data_fetch_mcp_url, {'fetch_customer_data'}),
                               (s.prediction_mcp_url, {'predict_churn', 'predict_fraud', 'predict_qoe'})):
        async with Client(str(endpoint), timeout=20) as client:
            names = {tool.name for tool in await client.list_tools()}
            print(sorted(names))
            assert names == expected
asyncio.run(asyncio.wait_for(main(), timeout=45))
PY
```

Also compare OGX's registered tools with this allowlist. The direct MCP listing does not by itself prove OGX registration, external LLM use or model inference.

For M02 set local `E2E_MODEL` to one of `churn`, `fraud`, `qoe`, and `E2E_SUBJECT_ID` to the actual matching ID from M01, validated against the appropriate latest billing/QoE source mapping. Run separately for all three families; never use a literal example ID as real data. The shipped client obtains feature rows through the intended MCP path:

```sh
oc exec -i deployment/api -c api -n telco-revenue-assurance-agent --request-timeout=50s -- env E2E_MODEL="$E2E_MODEL" E2E_SUBJECT_ID="$E2E_SUBJECT_ID" python - <<'PY'
import asyncio, math, os
from api.db.session import ApiSettings
from api.services.mcp_clients import McpClients

async def main():
    family, subject = os.environ['E2E_MODEL'], os.environ['E2E_SUBJECT_ID']
    assert family in {'churn', 'fraud', 'qoe'} and subject
    result = await getattr(McpClients.from_settings(ApiSettings()), 'predict_' + family)([subject])
    rows = result['predictions']
    assert len(rows) == 1
    row = rows[0]
    assert row['transaction_id' if family == 'fraud' else 'customer_id'] == subject
    assert isinstance(row['model_version'], str) and row['model_version']
    if family == 'qoe':
        assert type(row['score']) in (float, int) and math.isfinite(row['score']) and 0 <= row['score'] <= 100
    else:
        assert type(row['class_id']) is int
        if family == 'fraud':
            assert row['class_id'] in (0, 1)
            assert row['is_fraud'] is (row['class_id'] == 1)
            assert row['label'] == ('Fraud' if row['class_id'] == 1 else 'Non-Fraud')
    print(result)
asyncio.run(asyncio.wait_for(main(), timeout=45))
PY
```

Additionally validate all fields against `contracts/prediction-mcp.json` and the actual artifact metadata; the assertions above do not replace the full contract.

The actual destinations are `/v2/models/churn-prediction/infer`, `/v2/models/fraud-prediction/infer`, `/v2/models/starlink-qoe/infer`. Churn inputs use manifest-ordered FP32 features and return raw INT64 classes, not probabilities. Fraud class order is `[False, True]` (0 Non-Fraud, 1 Fraud). QoE requires the real scaler/encoders and FP32 `[batch,1,8]`, returning a 0–100 score. Maximum batch is 100. Inspect real serving metadata/artifact hashes before any direct model probe; tensor names/order/version must never be guessed. Capture sanitized service-side correlation without bearer tokens. A successful `/openapi.json` or ready pod is not M02 evidence.

## Authenticated browser request helpers

Use DevTools only after real OAuth login at the admitted frontend route. Requests retain the normal same-origin cookie boundary. Preserve IDs in sessionStorage before submitting; never paste credentials or spoofed identity headers. The following creates one real scoring operation and therefore is forbidden until the full write gate is satisfied:

```js
// S01: initialize once; preserve this key on retry/refresh.
const key = sessionStorage.getItem('telco-e2e-scoring-key') || crypto.randomUUID();
sessionStorage.setItem('telco-e2e-scoring-key', key);
const start = () => fetch('/api/v1/scoring-runs', {
  method: 'POST', credentials: 'same-origin', headers: { 'Idempotency-Key': key },
  signal: AbortSignal.timeout(5000)
});
const first = await start();
if (first.status !== 202) throw new Error(`Expected 202, got ${first.status}`);
const accepted = await first.json(); // Validate run_id against the UUID contract.
sessionStorage.setItem('telco-e2e-run-id', accepted.run_id);
const replay = await start();
if (replay.status !== 202 || (await replay.json()).run_id !== accepted.run_id) {
  throw new Error('Scoring idempotency assertion failed');
}
const read = path => fetch(path, { credentials: 'same-origin', signal: AbortSignal.timeout(10000) });
// S02/S03: invoke individual reads; follow returned next_cursor using encodeURIComponent.
await read(`/api/v1/scoring-runs/${accepted.run_id}`);
await read(`/api/v1/scoring-runs/${accepted.run_id}/results?limit=100`);
await read(`/api/v1/metrics?run_id=${accepted.run_id}`);
```

Compare response bodies with `contracts/openapi.yaml`, not just status codes. If a submission times out, retain the same idempotency key; do not create a new key to recover an ambiguous response. Automated executions must use assertions and capture sanitized actual fields rather than treating these exploratory DevTools expressions as assertions.

C01 questions, using the actual completed run and verified artifact semantics:

1. Which customers are in each raw churn class in the latest completed run, and what dollar weight is associated with each class? If semantic class labels are not verified, say so and do not infer probabilities.
2. Which customers overlap the verified churn class of interest and transactions classified Fraud? Give stable customer IDs and compare counts to persisted results; if none overlap, report that honestly.
3. Which recorded regions should the analyst review based on the latest fraud counts, churn class distribution and QoE scores? Explain the evidence, unknown class semantics, and the ranking rule; do not invent revenue/causal claims.

Verify answers with the actual scoped DB/MCP records and metrics, not a hard-coded expected customer count. A valid answer may report missing semantic information or no matches; it must not invent unavailable facts. Confirm that tool-requiring answers actually traverse the intended OGX/MCP path rather than accepting an ungrounded fluent response.

For C02, create/reuse a real session UUID (UI key `telco-revenue-assurance-chat-session`) and persist one request UUID before this call:

```js
const session = sessionStorage.getItem('telco-revenue-assurance-chat-session');
const request = sessionStorage.getItem('telco-e2e-chat-request') || crypto.randomUUID();
sessionStorage.setItem('telco-e2e-chat-request', request);
const message = 'Which customers have raw churn class scores in the latest completed run?';
const send = text => fetch(`/api/v1/chat/sessions/${session}/messages`, {
  method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ request_id: request, message: text }), signal: AbortSignal.timeout(120000)
});
// Run separately, only after session is a valid UUID and the full chat gate is ready.
// await send(message); then replay identical body; then send(message + ' Changed request.').
```

The contract permits 120 seconds for chat. Observe long requests in intervals under 60 seconds without submitting duplicate messages; at timeout retain the existing IDs and inspect persisted state. Require the matching `session_id`, `request_id`, nonempty `response_id`, `assistant_message` and `created_at`. Same request/content replay is a successful deduplication test; changed content must return 409. Do not record a missing token or blocked model as an observed chat failure when no request was made.

## Results handoff

After authorized execution, write `.tmp/telco-revenue-assurance-agent/debug/e2e-results.yaml` using the skill output schema, retaining every ID above, exact safe command/action, expected/actual, timestamps and prerequisite reasons. Include all blocked/skipped tests so a partial run cannot be reported as a full application pass. If an existing results file is present at that time, reuse its exact test inventory per the skill rather than silently dropping cases. Preserve the prior result file before replacing it for review.

Full application E2E requires successful OAuth browser access, actual data-fetch and all three model paths, completed persisted scoring with consistent UI/API/DB metrics, and grounded external-LLM chat with idempotent persistence. Until these are observed, report the deployment's verified foundation health and its remaining blockers separately. Do not create a results file containing fabricated PASS/FAIL entries during this preparation-only task.
