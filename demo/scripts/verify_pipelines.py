#!/usr/bin/env python3
"""
Poll KFP until all three pipeline experiments (churn, revass, starlink-qoe)
have their latest run in a terminal state.

Exits 0 when all runs SUCCEEDED.
Exits 1 when any run FAILED or when the timeout is reached.

Environment variables (same as run-pipeline-* targets):
  KUBEFLOW_ENDPOINT   KFP API base URL
  BEARER_TOKEN        OpenShift bearer token (oc whoami -t)

Usage:
  KUBEFLOW_ENDPOINT=https://... BEARER_TOKEN=... python demo/scripts/verify_pipelines.py
"""

import os
import sys
import time

import kfp

EXPERIMENTS = ["churn", "revass", "starlink-qoe"]
POLL_INTERVAL = 30   # seconds between polls
TIMEOUT = 2400       # 40 minutes total


def latest_run(client, experiment_name):
    """Return (run_id, state) for the most recent run in the experiment, or (None, None)."""
    try:
        exp = client.get_experiment(experiment_name=experiment_name)
    except Exception:
        return None, None
    runs = client.list_runs(
        experiment_id=exp.experiment_id,
        sort_by="created_at desc",
        page_size=1,
    )
    if not runs.runs:
        return None, None
    r = runs.runs[0]
    return r.run_id, r.state


def main():
    endpoint = os.environ.get("KUBEFLOW_ENDPOINT", "")
    token = os.environ.get("BEARER_TOKEN", "")
    if not endpoint or not token:
        print("ERROR: KUBEFLOW_ENDPOINT and BEARER_TOKEN must be set.", file=sys.stderr)
        sys.exit(1)

    client = kfp.Client(host=endpoint, existing_token=token, verify_ssl=False)

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        states = {}
        missing = []
        for exp in EXPERIMENTS:
            run_id, state = latest_run(client, exp)
            if run_id is None:
                missing.append(exp)
            else:
                states[exp] = state.lower() if state else "unknown"

        if missing:
            print(f"[wait] No runs yet for experiments: {missing}")
        else:
            summary = "  ".join(f"{e}={s}" for e, s in states.items())
            print(f"[poll] {summary}")
            failed = [e for e, s in states.items() if s == "failed"]
            if failed:
                print(f"ERROR: pipeline run(s) FAILED: {failed}", file=sys.stderr)
                sys.exit(1)
            if all(s == "succeeded" for s in states.values()):
                print("All pipeline runs SUCCEEDED.")
                sys.exit(0)

        elapsed = int(TIMEOUT - (deadline - time.time()))
        remaining = TIMEOUT - elapsed
        print(f"  Elapsed: {elapsed}s / {TIMEOUT}s. Next check in {POLL_INTERVAL}s.")
        time.sleep(POLL_INTERVAL)

    print(f"ERROR: timeout after {TIMEOUT}s waiting for pipeline runs.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
