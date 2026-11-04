# Conventions

- Use `customer_id`, `transaction_id`, and `run_id` consistently across packages and contracts. Keep model names and persisted status values aligned with their frozen contracts.
- Put orchestration, persistence, retries, or logic shared by multiple routes in a service. Keep handlers focused on request validation, calling a service, and mapping its result.
- Tests use package-local fixtures and upstream fakes. Do not connect to a live database, model, MCP service, or cluster in unit tests.
- Map exceptions into the shared error model at each protocol boundary. Preserve stable error codes and safe messages; keep provider diagnostics in server logs keyed by the request ID.
- Python packages extend `packages/configs/ruff.toml`; the root Makefile runs the shared strict mypy configuration. TypeScript packages extend `packages/configs/tsconfig.base.json` and use the shared ESLint and Prettier configurations.
