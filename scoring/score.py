#!/usr/bin/env python3
"""
Batch scoring job: runs churn + fraud predictions for all customers
and writes results back to the database.

Churn results  → customers.predicted_churn, churn_probability, predictions_scored_at
Fraud results  → transactions.predicted_fraud (latest transaction per customer)
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PREDICTOR_URL = os.environ.get("PREDICTOR_URL", "http://predictor.telco-revenue-assurance-agent.svc:8080")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))


def predict_batch(model: str, customer_ids: list) -> list:
    url = f"{PREDICTOR_URL}/predict/{model}/batch"
    payload = json.dumps({"customer_ids": customer_ids}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)["predictions"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Predictor returned HTTP {exc.code}: {body}") from None


def run():
    conn = psycopg2.connect(DATABASE_URL)

    with conn.cursor() as cur:
        cur.execute("SELECT customer_id FROM customers ORDER BY customer_id")
        customer_ids = [row[0] for row in cur.fetchall()]

    total = len(customer_ids)
    log.info("Found %d customers to score", total)

    # ── Churn scoring ──────────────────────────────────────────────────────────
    log.info("Scoring churn predictions in batches of %d...", BATCH_SIZE)
    churn_results = {}
    for i in range(0, total, BATCH_SIZE):
        batch = customer_ids[i : i + BATCH_SIZE]
        try:
            predictions = predict_batch("churn", batch)
            for pred in predictions:
                churn_results[pred["customer_id"]] = pred
        except Exception as exc:
            log.warning("Churn batch %d-%d failed: %s", i, i + BATCH_SIZE, exc)
        log.info("  churn: %d/%d", min(i + BATCH_SIZE, total), total)

    # ── Fraud scoring ──────────────────────────────────────────────────────────
    log.info("Scoring fraud predictions in batches of %d...", BATCH_SIZE)
    fraud_results = {}
    for i in range(0, total, BATCH_SIZE):
        batch = customer_ids[i : i + BATCH_SIZE]
        try:
            predictions = predict_batch("fraud", batch)
            for pred in predictions:
                fraud_results[pred["customer_id"]] = pred
        except Exception as exc:
            log.warning("Fraud batch %d-%d failed: %s", i, i + BATCH_SIZE, exc)
        log.info("  fraud: %d/%d", min(i + BATCH_SIZE, total), total)

    # ── QoE scoring ────────────────────────────────────────────────────────────
    log.info("Scoring QoE predictions in batches of %d...", BATCH_SIZE)
    qoe_results = {}
    for i in range(0, total, BATCH_SIZE):
        batch = customer_ids[i : i + BATCH_SIZE]
        try:
            predictions = predict_batch("qoe", batch)
            for pred in predictions:
                qoe_results[pred["customer_id"]] = pred
        except Exception as exc:
            log.warning("QoE batch %d-%d failed: %s", i, i + BATCH_SIZE, exc)
        log.info("  qoe: %d/%d", min(i + BATCH_SIZE, total), total)

    # ── Write churn results ────────────────────────────────────────────────────
    log.info("Writing %d churn + %d QoE results to customers table...", len(churn_results), len(qoe_results))
    churn_rows = [
        (pred["label"] == 1, pred["churn_probability"], qoe_results.get(cid, {}).get("qoe_score"), cid)
        for cid, pred in churn_results.items()
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            UPDATE customers
               SET predicted_churn        = %s,
                   churn_probability      = %s,
                   predicted_qoe_score    = %s,
                   predictions_scored_at  = NOW()
             WHERE customer_id = %s
            """,
            churn_rows,
            page_size=500,
        )
    conn.commit()
    log.info("Churn + QoE results committed.")

    # ── Write fraud results ────────────────────────────────────────────────────
    log.info("Writing %d fraud results to transactions table...", len(fraud_results))
    fraud_rows = [
        (pred["label"] == 1, cid)
        for cid, pred in fraud_results.items()
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            UPDATE transactions
               SET predicted_fraud = %s
             WHERE transaction_id = (
                   SELECT transaction_id FROM transactions
                    WHERE customer_id = %s
                    ORDER BY transaction_id DESC
                    LIMIT 1
             )
            """,
            fraud_rows,
            page_size=500,
        )
    conn.commit()
    log.info("Fraud results committed.")

    conn.close()

    log.info(
        "Scoring complete — churn: %d/%d  fraud: %d/%d  qoe: %d/%d",
        len(churn_results), total,
        len(fraud_results), total,
        len(qoe_results), total,
    )


if __name__ == "__main__":
    started = time.monotonic()
    run()
    log.info("Total time: %.1fs", time.monotonic() - started)
