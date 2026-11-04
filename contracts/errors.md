# Error contract

This file is the shared error contract for the public REST API, internal MCP tools,
OGX/vLLM OpenAI-compatible APIs, PostgreSQL clients, and KServe V2 clients. Public
errors contain safe messages and stable codes. SQL text, credentials, provider traces,
model artifact paths, and stack traces never reach a client.

## Public REST envelope

Every non-2xx response from `contracts/openapi.yaml` uses this exact JSON shape:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "limit must be between 1 and 100.",
    "request_id": "req_01J8Z2N5M9KA",
    "retryable": false,
    "details": [
      {
        "field": "limit",
        "reason": "Value must be between 1 and 100.",
        "limit": 100,
        "current_run_id": null
      }
    ]
  }
}
```

`error` is required. Its fields are all required: `code` and `message` are strings,
`request_id` is a correlation string, `retryable` is a boolean, and `details` is an
array. Each detail has `field` (string or null), `reason` (string), `limit` (integer
or null), and `current_run_id` (UUID string or null). Use an empty `details` array
when no safe field-level context applies. The HTTP status is authoritative; the body
does not repeat it.

## MCP tool error result

MCP tool failures use the MCP result error flag and a typed structured payload. The
text block repeats the safe message for clients that only render text:

```json
{
  "isError": true,
  "structuredContent": {
    "code": "MODEL_UNAVAILABLE",
    "message": "The requested model is temporarily unavailable.",
    "request_id": "req_01J8Z2N5M9KF",
    "retryable": true,
    "details": {}
  },
  "content": [
    {
      "type": "text",
      "text": "The requested model is temporarily unavailable."
    }
  ]
}
```

The `structuredContent` object has the same stable `code`, `message`, `request_id`,
and `retryable` fields as the REST envelope; `details` is an object and is empty when
there is no safe detail. An MCP error is not a successful tool result even when the
streamable HTTP transport itself returns HTTP 200. Prediction tool codes are defined
in `contracts/prediction-mcp.json`; data-fetch tool codes are defined in
`contracts/data-fetch-mcp.json`.

## OpenAI-compatible upstream error object

OGX and vLLM return this OpenAI-compatible shape for internal HTTP errors:

```json
{
  "error": {
    "message": "The configured model is temporarily unavailable.",
    "type": "server_error",
    "param": null,
    "code": "MODEL_UNAVAILABLE"
  }
}
```

`message`, `type`, `param`, and `code` are required. `param` and `code` may be null
when the upstream provider does not identify a field or stable code. The backend
normalizes upstream failures into the public REST envelope and never relays raw
provider diagnostics.

## KServe V2 error response

KServe V2 inference errors use the HTTP status and a JSON body containing a required
string `error` message. The prediction-tools adapter classifies the status and safe
message into a stable code in the table below. The adapter does not expose the raw
KServe body to REST or MCP callers.

```json
{
  "error": "Invalid input tensor shape"
}
```

## Error codes

Retry policy is from the caller's perspective. A retry must remain within the
calling interface's deadline and attempt cap. `Retry-After`, when present, takes
precedence over the default delay.

| Code | HTTP status / protocol | Raised when | Retry policy |
|---|---|---|---|
| `VALIDATION_ERROR` | 400 REST | A path, query, header, or request body fails API validation. | Never retry without changing the request. |
| `INVALID_REQUEST` | 400 OpenAI-compatible; MCP equivalent is `INVALID_INPUT` | An OGX or LLM request violates its supported request subset. | Never retry. |
| `INVALID_INPUT` | MCP error; HTTP equivalent 400 | A prediction tool input is malformed, empty, oversized, or invalid. | Never retry. |
| `AUTHENTICATION_OR_AUTHORIZATION` | 401 or 403 REST/OpenAI-compatible | The OAuth Route or configured internal service authentication rejects the caller. Database authentication failures map to `DATABASE_UNAVAILABLE` and fail closed. | Never retry; correct identity or permissions first. |
| `NOT_FOUND` | 404 REST | A requested scoring run does not exist. | Never retry for the same identifier. |
| `SUBJECT_NOT_FOUND` | MCP error; HTTP equivalent 404 | One or more requested customer or transaction identifiers do not exist, or no assigned QoE sample exists. The whole batch fails; no partial success is returned. | Never retry unless the source data changes. |
| `INVALID_MODEL_ID` | 404 OpenAI-compatible | The configured LLM model identifier is not served by the selected endpoint. | Never retry; correct deployment configuration. |
| `INVALID_MODEL_NAME` | 404 KServe V2; MCP equivalent `MODEL_UNAVAILABLE` | The requested KServe model name is not registered. | Never retry until deployment configuration is corrected. |
| `INVALID_TENSOR` | 400 KServe V2 | Input tensor name, datatype, dimensions, or feature width violates the model contract. | Never retry; correct the feature manifest or request. |
| `INVALID_ENTITY` | MCP error; HTTP equivalent 400 | A data-fetch request names an entity outside the allowlist. | Never retry. |
| `INVALID_FIELD` | MCP error; HTTP equivalent 400 | A field alias is unknown or is not allowed for the selected entity. | Never retry. |
| `INVALID_FILTER` | MCP error; HTTP equivalent 400 | A filter has an unsupported operator/value, an unapproved JSON path, or an invalid field combination. | Never retry. |
| `RESULT_LIMIT_EXCEEDED` | MCP error; HTTP equivalent 400 | A data-fetch limit, ID count, or result size exceeds the configured bound. | Never retry until the request is smaller. |
| `RUN_ALREADY_ACTIVE` | 409 REST | A different idempotency key attempts to start a run while one is queued or running. | Do not retry until the active run finishes; then use a new key. |
| `RUN_NOT_COMPLETE` | 409 REST | Results are requested before the run reaches completed state. | Retry only after the status endpoint reports completed; no fixed delay is promised. |
| `IDEMPOTENCY_CONFLICT` | 409 REST | A chat `request_id` is reused within one session for different message content. | Never retry with that request ID; submit changed content with a new ID. |
| `FEATURE_SCHEMA_MISMATCH` | MCP error; HTTP equivalent 502 | The model feature names, width, order, class metadata, or preprocessing version does not match the deployed artifact manifest. | Never retry until deployment artifacts/configuration are corrected. |
| `MODEL_NOT_READY` | 503 KServe V2; MCP equivalent `MODEL_UNAVAILABLE` | The model is registered but not ready to infer. | Prediction client may retry once after 500 ms; do not exceed the operation deadline. |
| `MODEL_UNAVAILABLE` | 503 MCP/OpenAI-compatible | Model endpoint is unavailable or a transient inference failure remains after the bounded adapter retry. | Prediction client retries once; OGX retries at most once; REST clients retry mapped `UPSTREAM_UNAVAILABLE` at most twice. |
| `PREPROCESSING_ARTIFACT_MISSING` | 503 KServe adapter; MCP equivalent `FEATURE_SCHEMA_MISMATCH` | QoE scaler or season/weather label-encoder sidecar is absent from the configured artifact prefix. | Never retry until the artifact prefix is corrected. |
| `STORAGE_ERROR` | 503 KServe adapter; MCP equivalent `MODEL_UNAVAILABLE` | KServe cannot read a configured model artifact from external S3-compatible storage. | Retry once only for a transient storage outage; configuration/authentication errors require correction. |
| `DATABASE_UNAVAILABLE` | 503 MCP/REST mapping; startup/health failure for connection setup | PostgreSQL is unavailable, not ready, or rejects the configured role/DSN. SQLite and in-memory fallback are forbidden. | Data-fetch may retry one transient failure; bad DSNs/permissions are not retried. |
| `DATA_INTEGRITY_ERROR` | 500 internal database/application error; public REST maps to `INTERNAL_ERROR` | A database primary-key, foreign-key, uniqueness, or check constraint fails outside a validated idempotent upsert. The transaction is rolled back. | Do not retry unchanged input; investigate the data or application invariant. |
| `RATE_LIMITED` | 429 OpenAI-compatible | OGX or the LLM endpoint throttles a request. | Retry once after `Retry-After`, within the OGX deadline and retry cap. |
| `UPSTREAM_UNAVAILABLE` | 502, 503, or 504 REST/OpenAI-compatible | A required MCP, OGX, model, or database dependency fails or misses its configured deadline. The public API maps provider-specific failures to this code. | REST client may retry at most twice with exponential backoff starting at 1 second and capped at 8 seconds; reuse idempotency/request IDs. |
| `UPSTREAM_TIMEOUT` | 504 OpenAI-compatible/MCP semantic code | An internal upstream call exceeds its bounded deadline. The public REST API maps it to `UPSTREAM_UNAVAILABLE`. | Retry only under the caller's bounded transient policy; at most one retry for prediction tools and OGX. |
| `INTERNAL_ERROR` | 500 REST/OpenAI-compatible | An unexpected error is not covered by a more specific code. | No automatic retry. Use `request_id` to report the failure. |

## Unexpected errors and logging

Unexpected errors use `INTERNAL_ERROR` and HTTP 500 where the protocol has HTTP
status codes. Log the request ID, component, stable error code, and server-side
exception details. Never include raw SQL, credentials, Secret contents, arbitrary
model URLs, prompt internals, full source rows, or stack traces in a client response.
For a failed scoring run, persist only a safe `error_code`; operator diagnostics stay
in server logs keyed by the run and request identifiers.

## Retry and idempotency rules

- Repeating `POST /scoring-runs` must reuse the same `Idempotency-Key`; it returns
  the same persisted run. A distinct key cannot start a concurrent run.
- Repeating a chat message must reuse the same `session_id` and `request_id`. The
  same request ID and content return the original response; changed content conflicts.
- GET operations are safe to retry within the response deadline.
- Invalid input, authorization, missing records, schema mismatches, and configuration
  errors are not made transient by retrying.
- MCP and KServe errors are normalized at the calling adapter before crossing the
  public API boundary.
