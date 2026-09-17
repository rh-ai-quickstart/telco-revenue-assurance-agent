# Telco Revenue Assurance Agent

**Issue:** Jira (not yet created — project not specified)
**Slug:** telco-revenue-assurance-agent
**Date:** 2026-09-16

Product title: **Telco Revenue Assurance Agent**. README and similar user-facing docs should use the full title: **Telco Revenue Assurance Agent: Quality, Churn & Fraud Insights**.

## Use case summary

A dashboard that shows aggregated predictions from three ML models, paired with a chatbot agent that turns those predictions into business insight by cross-referencing the models against different customer segments.

Each model answers a narrow question: "will this customer churn?", "is this transaction fraud?", "what's the QoE at this location?" Individually, these are predictions — information, not insight. The dashboard aggregates them into a unified view. The agent turns them into insight — it can slice customers by region, plan type, or risk level, run them through multiple models, and find correlations that no single model can surface on its own.

The three models already work in isolation. The value of putting them behind one agent comes from the fact that churn, fraud and service quality are owned by different teams inside a telco, funded from different budgets, and rarely reconciled against each other. What the agent adds over the three models running separately: root cause across domains in a single query; ranking by revenue at risk using each customer's dollar weight; and fraud triage that weighs that dollar weight (and churn risk) before suggesting an account block. Dollar weight is the mean of `TotalBillAmount` across that customer's rows in [Omer1234567/telco-churn-synthetic](https://huggingface.co/datasets/Omer1234567/telco-churn-synthetic) — synthetic bill dollars, not live ARPU.

The primary user of the dashboard and chatbot is a revenue-assurance / churn / fraud **analyst**. The quickstart itself does not produce operator savings; it gives a running reference on OpenShift AI that can be pointed at their own data. "Evaluated" in v1 means the canned demo questions work against the synthetic store and the three prediction tools. There is no separate eval product or ROI study.

Source experiments (prediction contracts as in Telco-AIX): Churn Prediction, Revenue Assurance and Fraud Management (RAFM), and Starlink Internet Service Quality of Experience Predictions.

## User flows

### Primary flow

1. The analyst opens the dashboard and triggers an **on-demand** scoring run (no schedule). That run sends the combined synthetic customer and transaction data through the three prediction tools (`predict_churn`, `predict_fraud`, `predict_qoe`) and stores the results.
2. The dashboard shows summary metrics from those results: overall churn rate, fraud count, average QoE by region, and the worst region (whichever region the generator made the outlier — it is **not** required to be "West").
3. The analyst asks the chatbot a "why?" question, for example: "Why is the churn rate high? Where are we losing customers?"
4. The agent investigates: filters/groups customers (for example by region) via a database query tool, then calls churn / fraud / QoE prediction tools on the relevant slices.
5. The agent presents insight: where the problem is concentrated, whether QoE or fraud is a factor, how many customers are at risk, those customers ranked by dollar weight (revenue at risk), and a suggested next action (for example investigate coverage or target retention offers). On fraud overlap, triage weighs dollar weight and churn risk before suggesting a review — the agent suggests; it does not execute retention offers or account blocks.

The chat path should work and feel reasonable. v1 has **no** numeric latency target in requirements or acceptance criteria.

### Other example questions (canned demo path)

- "Are fraud-flagged customers also churning?" — overlap of fraud flags and churn risk, weighed by dollar weight; review before blocking accounts.
- "Which region should we worry about most?" — QoE, churn, and fraud together, with at-risk customers ranked by dollar weight, to name a priority region.

## Data model

The project works with synthetic data from the three experiment datasets, not live operator data.

**Sources (keep all original rows — models trained on this kind of data):**

| Source | Dataset | Size |
|--------|---------|------|
| Churn | [Omer1234567/telco-churn-synthetic](https://huggingface.co/datasets/Omer1234567/telco-churn-synthetic) | ~113k rows (112,552 billing-cycle records with an existing `CustomerID`; several cycles per customer) |
| QoE | [fenar/starlink](https://huggingface.co/datasets/fenar/starlink) | 100k location samples |
| RAFM | [fenar/revenue_assurance](https://huggingface.co/datasets/fenar/revenue_assurance) | 1M transaction rows |

RAFM scores individual transactions rather than customers, so a single flat customer record cannot serve all three models. Persistent store is two tables:

- **customers** — churn profile from the churn dataset (existing `CustomerID` is the join key), plus location (`lat`/`lon`), region, and `season` / `weather` for QoE. Plan type and similar metadata as needed to slice the agent queries. Each customer has a **dollar weight**: the mean of `TotalBillAmount` across **all** of that `CustomerID`'s billing-cycle rows in the churn dataset. Store it as a customer-level field; do not rewrite the original per-cycle `TotalBillAmount` values. This average is independent of latest-cycle churn scoring.
- **transactions** — original RAFM rows (call duration, data usage, SMS, roaming, mobile wallet, cost, location distance, PIN, averages, fraud label, and related fields) with a `customer_id` added so **several transactions map to one customer**.

**Join / generation rules:**

- Faithful to original feature values. Do not rewrite churn or RAFM labels or trained-on columns just to make a story.
- Link with `customer_id`. Churn already has `CustomerID`; RAFM does not — assign RAFM rows many-to-one onto those IDs. Transaction counts per customer should stay reasonable.
- Per-customer consistency: columns that should not change across one customer's transactions (for example `Plan_Type` on RAFM) must be consistent for that `customer_id`. Exact which columns and how to enforce that is left to the database-generation task.
- Draw **original** Starlink coordinates (`latitude`, `longitude`) and assign them to customers, grouped into regions so nearby locations form an area and QoE can vary by region. Do not invent coordinates outside that empirical distribution.
- **Generate** `season` and `weather` — do not copy them from the same Starlink row as the coordinates. The store is a synthetic snapshot of each area at one time: customers in the same area get the **same or close** season and weather. Use Starlink's empirical season/weather classes (the values the QoE model was trained on), not new labels.
- Plant a **tunable** regional coupling between poor QoE and higher churn when assigning those location/region fields (not by changing original churn/RAFM labels). Close locations stay together as one area. The outlier region is **not** required to be West; the original doc's West walkthrough is illustrative only.
- No empty required fields. If a needed field is empty, fill it from the same empirical probability as the rest of that column so it stays consistent with the synthetic world.
- **Dollar weight:** for each `CustomerID`, set dollar weight to the mean of original `TotalBillAmount` over that customer's churn rows. Do not invent bills or convert currency. Do not change per-cycle `TotalBillAmount` (it remains a trained-on feature).
- **Churn scoring:** a `CustomerID` has several billing-cycle rows. Dashboard scoring and `predict_churn` use the **latest `BillingCycleStart` per customer**. All cycle rows stay in the store (and all of them feed the dollar-weight average). The agent does **not** pick a month in v1. Future: `predict_churn` may take a month so the agent can score a specific cycle.

**Lifecycle:** generate/join the synthetic store → analyst-triggered on-demand score through the prediction tools → store predictions for the dashboard → agent reads the store and calls the same tools on slices for follow-up questions.

Current source-data limitation: Churn and RAFM use synthetic telecom data; Starlink currently relies on satellite internet data. Training all three models exclusively on synthetic telecom QoE data is a future enhancement.

## AI touchpoints

- **Capability:** Agent (tool-calling / ReAct-style). **Why:** The analyst's question determines which slices and which models to consult; the LLM chooses tool order. Tools in v1: query the customer/transaction store; `predict_churn`; `predict_fraud`; `predict_qoe`.
- **Capability:** Classification / tabular ML (existing churn and RAFM models). **Why:** Per-customer churn prediction and per-transaction fraud flags that the dashboard aggregates and the agent cross-references.
- **Capability:** QoE prediction (existing Starlink model). **Why:** Location-level quality scores the agent can compare with churn and fraud.
- **Capability:** Text generation (LLM). **Why:** Turn tool results into analyst-facing insight and a suggested action.

**Agent tools hide serving (and caching).** The agent does not call a model HTTP API and does not know Flask vs KServe, path names, or wire formats. `predict_churn`, `predict_fraud`, and `predict_qoe` own that connection: they take domain inputs (customer / transaction / location fields) and return domain results (e.g. Will Churn / Will Not Churn, Fraud / Non-Fraud, QoE numbers). The tools may cache repeated predictions for the same inputs to avoid redundant model calls, even when those calls are cheap. If a model is later served with KServe V2, the tool — not the agent — maps JSON fields into the V2 tensor payload and maps numeric outputs (0 / 1) back to labels. Swapping a backend or adding caching must not change the agent or the tool schemas.

**Model considerations:**

- The three experiment models keep their existing trained artifacts in v1 (no retraining). Whether each one stays on its current Flask server or moves to OpenShift AI native serving (for example KServe with MLServer, OpenVINO Model Server, Triton, or a custom ServingRuntime) is **not frozen here**. The developer who investigates that experiment decides, based on what fits that model (artifact format, preprocessing, and whether a native runtime can take it without a rewrite).
- Source experiment APIs (Flask `/predict-lgbm`, `/predict`, and so on) are the starting point, not a contract the agent is bound to. The frozen contract for the agent is the tool I/O above.
- LLM: specific model left to architecture. Default is on-cluster if the cluster can host it (source note sized around a single L4/A10). If the cluster cannot support that, use a remote endpoint. GPU is optional.
- No multilingual or extra safety-model requirement stated for v1.
- New value is orchestration, not retraining the three experiment models in v1.

Requirement mapping (context for architecture, not a stack decision): agent-with-tools over a shared store; three prediction **tools** in front of on-cluster experiment models (Flask or native serving, chosen per model); LLM on-cluster with remote fallback; web UI for the analyst; persistent customer + transaction store plus a synthetic generator/join; on-demand scoring, not a schedule.

## Deploy target

- **Primary:** OpenShift AI only
- **Secondary:** none (no local podman)
- **GPU:** Optional — default to on-cluster LLM when the cluster has resources; otherwise remote endpoint. The three experiment models run on the OpenShift AI cluster; Flask vs native serving is a per-model implementation choice (see AI touchpoints).
- **Scale:** Demo / reference using the **full** combined experiment sizes (~113k churn rows, 100k Starlink samples, 1M RAFM transactions). Not concurrent multi-tenant operator load. No committed latency number.

Deployed pieces at requirement level: the three experiment models (each served however that model's investigation chose), a new agent + dashboard app, an LLM (on-cluster or remote), a customer/transaction database, unified Helm chart (model endpoints from configuration, not hardcoded host:port). Scoring and the agent call the prediction **tools**, not a raw model URL.

## Constraints and non-goals

The project uses synthetic data from the three experiment datasets. It does not ingest live operator data.

v1 will NOT:

- Include the Service Assurance Insights/Predictions experiment (notebook only, no API today)
- Add a live weather/season tool (season and weather are generated customer fields, aligned so the same area at the same synthetic time has the same or close values)
- Add automated alerts when metrics cross thresholds
- Retrain all three models exclusively on synthetic telecom QoE data
- Provide local podman-compose as a deploy target
- Run scheduled rescoring (analyst-triggered on-demand only)
- Ship the existing Starlink interactive map UI as the product dashboard (v1 dashboard is summary metrics + chatbot)
- Claim CFCA / TM Forum / carrier churn dollar figures as quickstart outcomes (dollar weight is the synthetic mean of `TotalBillAmount`, not those industry figures)
- Put a numeric chat-latency SLA in requirements or acceptance criteria
- Ship a separate evaluation product, agent-eval suite, or ROI study ("evaluated" = canned demo path works)
- Let the agent select a billing month for churn scoring (v1 is latest cycle per customer only)

v1 WILL document the planted QoE–churn regional coupling as intentional and tunable, and will not hard-code that the outlier region is West (like the example).

Issue tracking for this work is Jira (project/instance to be filled later), not the GitHub quickstart backlog.

## Open questions

- [ ] Which Jira project/instance should the tracking ticket live in? (affects: header / issue destination)
- [ ] Exact per-customer RAFM consistency rules (which columns must be constant on one `customer_id`) — deferred to the database-generation task (affects: Data model)
