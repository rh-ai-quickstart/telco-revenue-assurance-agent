# Deployment report: telco-revenue-assurance-agent

**Status: Basic chat fixed; prediction setup incomplete.**  
**Namespace:** `telco-revenue-assurance-agent`  
**Chat verified:** 2026-10-25 15:50:48 UTC  
**Latest full health scan:** 2026-10-25 15:48:11 UTC

Basic chat is deployed on OpenShift. A fresh “hi” received a reply and was saved correctly; retry and conversation-memory checks also passed. The first data load has completed. Prediction features still require the remaining trained model files and storage settings.

[Open the app](https://telco-revenue-assurance-agent-telco-revenue-assurance-agent.apps.ai-dev02.kni.syseng.devcluster.openshift.com). Refresh the page and send “hi”. The computer was used to test and package the fix; OpenShift runs the app.

## Current state

| Part | State |
| --- | --- |
| Website | Frontend Ready; existing TLS and OAuth entrypoint checks passed |
| AI connection | External LLM configured using `telco-remote-llm`; OGX and app server redeployed and Ready |
| Chat | Four live checks passed through the frontend nginx proxy |
| Database and first data load | Ready; schema/permission checks passed and original seed Job Complete |
| Predictions | Blocked by missing trained models, feature artifacts, storage and authentication settings |
| Helm release | Original revision 1 still records failed; the scoped fixes did not run a full upgrade |

The scan observed **5/6 application workloads Ready**, **66 healthy resources**, **14 unhealthy or missing prediction dependencies**, and **4 retained historical failed migration Pods**, across 84 resources. All 16 current Pod specifications have zero `quay.io/rh-ai-quickstart/` application image references.

## Chat repair and validation

The app used an unsupported upstream conversation identifier and saved a retry's empty response incorrectly. The repair uses stored response history with logical-session metadata and correct SQL NULL handling; public API IDs, database schema and migration remain unchanged. These behaviors follow the [OGX Responses API](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/working_with_ogx/ogx-apis_rag) and [SQLAlchemy JSON handling](https://docs.sqlalchemy.org/en/21/core/type_basics.html).

- 39 focused local tests passed, including request retries, response history, schema parity and the public API contract.
- The new API image was built and published as `quay.io/yelias/api:0.1.0-chat-7aedd95bd5ef`; its exact digest was verified in both API containers, with all other Deployment settings preserved.
- The same previously rejected test request now returns HTTP 200 and saves its reply.
- Repeating it returns the same saved reply without changing the database.
- A second message recalled the earlier test value without that value in its input.
- A fresh literal “hi” returns HTTP 200 with a nonempty, saved reply from the configured external model.

These are internal frontend-proxy integration checks. An authenticated public-browser session and full prediction/scoring journeys remain unverified. The original failed chat and persistence-error reproduction are preserved as evidence.

## Review and evidence

This repair changes three API source files, two API test files, the internal OGX contract and two chart files. The chart supports an API-only image tag for this run. Earlier recovery changes and unrelated work were preserved.

- [Chat verification](/home/yelias/Desktop/Projects/quickstart-factory/.tmp/telco-revenue-assurance-agent/debug/external-llm-redeploy/final-chat-verification.json)
- [Image and rollout evidence](/home/yelias/Desktop/Projects/quickstart-factory/.tmp/telco-revenue-assurance-agent/debug/external-llm-redeploy/e2e-attempt-1/deployment-staging/build-result.json)
- [Focused test result](/home/yelias/Desktop/Projects/quickstart-factory/.tmp/telco-revenue-assurance-agent/debug/external-llm-redeploy/e2e-attempt-1/focused-tests-final-result.json)
- [Current deployment state](/home/yelias/Desktop/Projects/quickstart-factory/.rhoai-qs/telco-revenue-assurance-agent/.qs-factory/pipeline/deploy-state.yaml)
- [API fix history](/home/yelias/Desktop/Projects/quickstart-factory/.tmp/telco-revenue-assurance-agent/debug/fix-api.yaml)
- [Previous deployment report](/home/yelias/Desktop/Projects/quickstart-factory/.tmp/telco-revenue-assurance-agent/debug/deploy-report-before-external-llm-chat-fix.md)
