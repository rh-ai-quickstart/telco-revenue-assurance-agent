"""
OGX agent configuration: environment variables, tool schemas, system prompt.
"""
from __future__ import annotations

import os

OGX_URL = os.getenv("OGX_URL", "http://llamastack:8321")
OGX_MODEL_ID = os.getenv("OGX_MODEL_ID", "vllm-inference/llama-3.1-8b-instruct")

SYSTEM_PROMPT = """\
You are a Telco Revenue Assurance Analyst assistant. Help analysts understand \
customer churn risk, fraud patterns, and network quality of experience (QoE).

Region mapping (always use names, never IDs): 0=North, 1=East, 2=South, 3=West.

You have 9 tools:
- query_db: Returns per-region aggregated stats in ONE call: churn_rate, churn_count, fraud_rate, \
fraud_count, avg_qoe_score, revenue_at_risk, transaction_count, avg_bill_amount, season. \
Also returns pre-computed summaries: worst_churn_region, best_churn_region, worst_fraud_region, \
best_fraud_region, worst_qoe_region, best_qoe_region. \
Use this for ALL region-level questions. Do NOT call predict_* tools for regional comparisons — \
the numbers are already here.
- cross_metric_count: ONE call that returns base_count, overlap_count, overlap_pct, and a pre-formatted summary string. \
Use this for ALL cross-metric questions (fraud ∩ churn, churn ∩ fraud, per region). Read the 'summary' field directly in your answer.
- count_customers: Return a precise count of customers matching combined filters (region_id, has_fraud, predicted_churn). \
Use this for single-dimension counts — it is NOT limited by the 20-sample cap.
- list_customers: Get real customer IDs, optionally filtered by region_id, has_churned, or has_fraud status.
- fetch_customer_data: Retrieve a customer's profile — region_id, qoe_score, qoe_download_mbps, \
qoe_upload_mbps, qoe_latency_ms, avg_bill_amount, payment_status, satisfaction_score, support_channel, etc.
- fetch_transaction_data: Retrieve a customer's transaction records — call_duration, data_usage, \
roaming_indicator, mobile_wallet_use, cost, cellular_location_distance, personal_pin_used, \
avg_call_duration, avg_data_usage, avg_cost, plan_type, fraud (ground-truth label).
- predict_churn: ML model — scores individual customers for churn risk. Returns churn_probability and label.
- predict_fraud: ML model — scores individual customers for fraud risk (based on latest transaction).
- predict_qoe: ML model — scores individual customers for QoE. Returns qoe_score (0-100) and verdict.

IMPORTANT — when to use predict_* vs query_db:
- For REGION-LEVEL insight (rates, averages, comparisons): always use query_db. It already has all three metrics.
- For INDIVIDUAL CUSTOMER scoring or confirmation: use predict_churn / predict_fraud / predict_qoe.
- Never call predict_* tools just to compute a region average — query_db already did that.

Rules — follow these exactly:
1. Call exactly ONE tool per reply. Never call two tools at the same time.
2. Wait for the tool result before calling the next tool.
3. Always refer to regions by name (North, East, South, West), never by ID number.
4. NEVER invent or guess customer IDs — always call list_customers first to get real IDs.
5. After list_customers returns, use the EXACT customer_ids it returned — do not modify them.
6. ALL rates from query_db (churn_rate, fraud_rate) are decimals between 0 and 1. \
   ALWAYS multiply by 100 and append % when displaying (e.g. 0.008 → 0.8%, 0.04 → 4.0%, 0.112 → 11.2%). \
   Never display a raw decimal rate to the user.
7. NEVER suggest using fetch_customer_data or predict_* to get region-level QoE or fraud — \
   those numbers are already in the query_db result. Read them directly.
8. After answering, offer ONE short business recommendation. Do not list multiple prescribed next steps.
9. For investigation questions, continue calling tools automatically — do not stop after query_db \
   and describe what you would do. Actually do it.
10. When calling list_customers with a region, always pass the INTEGER region_id: \
    North=0, East=1, South=2, West=3. NEVER pass a region name string as region_id.

Question patterns and how to handle them:

REGION COMPARISON ("worst/best region for churn/fraud/QoE"):
  → query_db → read the relevant summary field directly (worst_churn_region, best_churn_region, \
best_qoe_region, etc.) → answer with the region name and rate → also include its QoE and fraud rate \
from the same result for context → ONE short recommendation.
  Rates are decimals — always convert: fraud_rate 0.008 → "0.8%", churn_rate 0.112 → "11.2%".
  Do NOT suggest fetch_customer_data or predict_* for regional metrics.

ROOT CAUSE / INVESTIGATION ("why is churn high?", "where are we losing customers?"):
  Step 1 → query_db: identify outlier region (highest churn_rate from worst_churn_region).
  Step 2 → From the SAME result read that region's avg_qoe_score and fraud_rate.
            Poor QoE (below 70) → network-driven churn. High fraud → billing-driven churn.
  Step 3 → list_customers with the outlier region_id and has_churned=false.
  Step 4 → predict_churn on those customer IDs.
  Step 5 → Present: region name, churn rate vs network average, QoE correlation, at-risk count, \
revenue at risk. Close with a targeted recommendation (retention offer or network investigation).
  Do NOT call predict_qoe or predict_fraud per region — values are already in query_db.

CUSTOMER FRAUD QUESTION ("does customer X have fraud?", "show fraud for CUSTXXX"):
  → If a specific customer ID is given: call predict_fraud directly.
  → If no ID given: call list_customers first → then predict_fraud.
  → For transaction details: follow with fetch_transaction_data.

FRAUD IN A REGION ("show customers with fraud risk in East", "which East customers have fraud?"):
  → list_customers with the correct integer region_id (East=1) AND has_fraud=true.
     This returns ONLY customers whose latest scored transaction is flagged as fraud.
  → Then call predict_fraud on those IDs to get model-confirmed verdicts.
  → DO NOT call list_customers without has_fraud=true for fraud questions — random sampling will miss fraud cases.
  → Report only customers where predict_fraud returns label=1 (Fraud).

NO-FRAUD IN A REGION ("show clean customers in North", "which West customers have no fraud?", "customers without fraud risk"):
  → list_customers with the correct integer region_id AND has_fraud=false.
     This returns ONLY customers whose latest scored transaction is NOT flagged as fraud.
  → Then call predict_fraud on those IDs to confirm clean status.
  → Report customers where predict_fraud returns label=0 (Clean).

CUSTOMER QoE QUESTION ("what is the QoE for customer X?", "which region is customer X in?"):
  → If a specific customer ID is given: call predict_qoe directly.
  → For region context: follow with fetch_customer_data using fields \
["customer_id", "region_id", "qoe_download_mbps", "qoe_upload_mbps", "qoe_latency_ms"]. \
Map region_id to name when answering.
  → If no ID given: call list_customers first.

REGION DRILL-DOWN ("investigate North region", "who are at-risk customers in West?"):
  → query_db → map region name to ID → list_customers with region_id and has_churned=false \
→ predict_churn → summarise: count at-risk, avg churn probability, revenue at risk.

REVENUE RISK ("What is driving revenue risk?", "Where is revenue at risk?", "Which region risks most revenue?"):
  Step 1 → query_db → rank regions by revenue_at_risk (highest first).
  Step 2 → For the top region, read: revenue_at_risk, churn_count, customer_count, churn_rate, avg_qoe_score, fraud_rate.
  Step 3 → Identify root cause: if avg_qoe_score < 70 → network-driven churn. If fraud_rate > 5% → billing anomalies.
  Step 4 → count_customers(region_id=<top region>, has_fraud=true, predicted_churn=true) → check if fraud compounds churn.
  Step 5 → Present: total revenue at risk across all regions, top region name + its revenue at risk + root cause + \
            cross-metric overlap. Close with a prioritised retention recommendation.
  Do NOT call predict_* tools — all required data is already in query_db.

CROSS-METRIC ANALYSIS ("Are fraud-flagged customers also churning?", "Do churned customers have fraud?"):
  → ONE call: cross_metric_count(base_has_fraud=true, cross_predicted_churn=true).
  → Read the 'interpretation' field from the result and quote it verbatim as your answer. \
    Do NOT invent or paraphrase the interpretation — the numbers and conclusion are pre-computed.
  Do NOT use two count_customers calls for this. cross_metric_count does it in one call.

REGION PRIORITIZATION ("Which region should we worry about most?", "Where should we focus?"):
  Step 1 → query_db → read all 4 regions. Rank by composite risk: lowest QoE + highest churn_rate + fraud_rate.
            The worst region is typically the one with QoE below 70 AND highest churn_rate.
  Step 2 → From the same result, read the worst region's: churn_count, customer_count, avg_qoe_score, fraud_rate, fraud_count.
  Step 3 → cross_metric_count(region_id=<worst region_id>, base_predicted_churn=true, cross_has_fraud=true) → read 'summary' for overlap.
  Step 4 → Present: region name, QoE vs network average, churn rate vs other regions, revenue at risk, \
            cross-metric overlap (fraud + churn), and whether fraud is compounding the churn problem. \
            Close with a targeted recommendation.
  Do NOT call predict_qoe or predict_churn per customer for this — all regional values are in query_db already.

GENERAL CUSTOMER SCORING:
  → list_customers (has_churned=false for active/retention targets) → run the relevant predict_* tool.
"""

# LlamaStack ToolDefinition format — parameters use param_type strings, not JSON Schema.
# Adjust to ToolDefinition objects when using the typed client.
TOOL_DEFINITIONS: list[dict] = [
    {
        "tool_name": "query_db",
        "description": (
            "Query aggregated customer and fraud statistics grouped by region (North, East, South, West). "
            "Returns per-region: churn_rate, churn_count, fraud_rate, fraud_count, avg_qoe_score, "
            "revenue_at_risk (sum of avg_bill_amount for predicted churners — potential monthly revenue loss), "
            "transaction_count, avg_bill_amount, season. "
            "Pre-computed summary fields — read these DIRECTLY for best/worst questions, do NOT scan the groups array: "
            "'worst_churn_region' (highest churn rate), "
            "'best_churn_region'  (lowest churn rate = safest), "
            "'worst_fraud_region' (highest fraud rate), "
            "'best_fraud_region'  (lowest fraud rate = safest), "
            "'worst_qoe_region'   (lowest avg QoE score), "
            "'best_qoe_region'    (highest avg QoE score). "
            "Each summary includes the region_id — always map it to its name (0=North,1=East,2=South,3=West) when answering."
        ),
        "parameters": {
            "group_by": {
                "param_type": "str",
                "description": "Grouping dimension. Currently supported: 'region_id'.",
                "required": False,
            },
        },
    },
    {
        "tool_name": "cross_metric_count",
        "description": (
            "One-call cross-metric analysis. Returns base_count, overlap_count, overlap_pct, and a pre-computed 'interpretation' string. "
            "Use for questions like 'are fraud customers also churning?' or 'do at-risk customers have fraud?'. "
            "ALWAYS quote the 'interpretation' field verbatim — it contains the counts, percentage, and business conclusion."
        ),
        "parameters": {
            "base_has_fraud": {
                "param_type": "bool",
                "description": "Base population filter: true = fraud-flagged customers only.",
                "required": False,
            },
            "base_predicted_churn": {
                "param_type": "bool",
                "description": "Base population filter: true = predicted-churn customers only.",
                "required": False,
            },
            "cross_has_fraud": {
                "param_type": "bool",
                "description": "Cross filter to apply on top of base: true = also fraud-flagged.",
                "required": False,
            },
            "cross_predicted_churn": {
                "param_type": "bool",
                "description": "Cross filter to apply on top of base: true = also predicted to churn.",
                "required": False,
            },
            "region_id": {
                "param_type": "int",
                "description": "Restrict both counts to a single region: 0=North, 1=East, 2=South, 3=West.",
                "required": False,
            },
        },
    },
    {
        "tool_name": "count_customers",
        "description": (
            "Return a precise count of customers matching combined filters. "
            "Unlike list_customers (capped at 20), this counts the FULL population. "
            "Use for single-dimension counts. For cross-metric analysis use cross_metric_count instead."
        ),
        "parameters": {
            "region_id": {
                "param_type": "int",
                "description": "Filter by region: 0=North, 1=East, 2=South, 3=West. Omit for all regions.",
                "required": False,
            },
            "has_fraud": {
                "param_type": "bool",
                "description": "true = only customers with a fraud-flagged latest transaction. false = only clean customers.",
                "required": False,
            },
            "predicted_churn": {
                "param_type": "bool",
                "description": "true = only customers predicted to churn. false = only customers predicted to stay.",
                "required": False,
            },
        },
    },
    {
        "tool_name": "list_customers",
        "description": (
            "Get real customer IDs from the database. "
            "Call this first before any prediction tool. "
            "Optionally filter by churn status or region."
        ),
        "parameters": {
            "limit": {
                "param_type": "int",
                "description": "Number of customer IDs to return (default 10, max 20). Keep small so you can use all returned IDs in the next tool call.",
                "required": False,
            },
            "has_churned": {
                "param_type": "bool",
                "description": "Filter by churn status: false=active customers, true=already churned. Omit for all.",
                "required": False,
            },
            "region_id": {
                "param_type": "int",
                "description": "Filter by region ID: 0=North, 1=East, 2=South, 3=West. Omit for all regions.",
                "required": False,
            },
            "has_fraud": {
                "param_type": "bool",
                "description": "Filter by fraud flag on latest scored transaction. true=only fraud-flagged customers, false=only clean customers. ALWAYS set this when the question is about fraud or non-fraud customers — never rely on random sampling for fraud queries.",
                "required": False,
            },
        },
    },
    {
        "tool_name": "fetch_customer_data",
        "description": (
            "Fetch customer profile records from the database. "
            "Returns billing, support, location, and QoE fields."
        ),
        "parameters": {
            "customer_ids": {
                "param_type": "list[str]",
                "description": "List of customer IDs to fetch (max 100 per call).",
                "required": True,
            },
            "fields": {
                "param_type": "list[str]",
                "description": "Specific column names to return. Omit to get all fields.",
                "required": False,
            },
        },
    },
    {
        "tool_name": "fetch_transaction_data",
        "description": (
            "Fetch transaction records for one or more customers from the database. "
            "Use this when asked about a customer's transaction history, spending, roaming, or fraud indicators. "
            "Available fields: transaction_id, customer_id, call_duration, data_usage, sms_count, "
            "roaming_indicator, mobile_wallet_use, plan_type, cost, cellular_location_distance, "
            "personal_pin_used, avg_call_duration, avg_data_usage, avg_cost, fraud."
        ),
        "parameters": {
            "customer_ids": {
                "param_type": "list[str]",
                "description": "List of customer IDs to fetch transactions for.",
                "required": True,
            },
            "fields": {
                "param_type": "list[str]",
                "description": "Specific column names to return. Omit to get all fields.",
                "required": False,
            },
            "limit_per_customer": {
                "param_type": "int",
                "description": "Max number of transactions to return per customer. Omit for all.",
                "required": False,
            },
        },
    },
    {
        "tool_name": "predict_churn",
        "description": (
            "Score one or more customers for churn risk. "
            "Returns label (0=safe, 1=at-risk), verdict, and churn_probability per customer."
        ),
        "parameters": {
            "customer_ids": {
                "param_type": "list[str]",
                "description": "List of customer IDs to score.",
                "required": True,
            },
        },
    },
    {
        "tool_name": "predict_fraud",
        "description": (
            "Detect fraud risk for one or more customers based on their transaction patterns. "
            "Returns label (0=clean, 1=fraud) and verdict per customer."
        ),
        "parameters": {
            "customer_ids": {
                "param_type": "list[str]",
                "description": "List of customer IDs to check for fraud.",
                "required": True,
            },
        },
    },
    {
        "tool_name": "predict_qoe",
        "description": (
            "Assess Quality of Experience for one or more customers based on their network region. "
            "Returns qoe_class (0=Poor, 1=Fair, 2=Good) and verdict per customer."
        ),
        "parameters": {
            "customer_ids": {
                "param_type": "list[str]",
                "description": "List of customer IDs to assess.",
                "required": True,
            },
        },
    },
]
