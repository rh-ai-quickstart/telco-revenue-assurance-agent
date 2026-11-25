# Telco Data Generator

Generates synthetic telco customer and transaction data from three HuggingFace
datasets and seeds a PostgreSQL database. Deployed as a one-shot Kubernetes Job
via Helm on OpenShift AI.

---

## Quick local test (no database required)

```bash
pip install -r data_generator/requirements.txt

python -m data_generator.generate \
  --n-customers 500 \
  --n-transactions 5000 \
  --n-regions 4 \
  --coupling 0.7 \
  --dry-run
```

Add `--save-csv` to write `customers.csv` and `transactions.csv` to the current directory for inspection.

---

## Deploy on OpenShift

### 1. Build and push the container image

```bash
cd data_generator/

docker build -t quay.io/<your-org>/telco-data-generator:1.0.0 .
docker push quay.io/<your-org>/telco-data-generator:1.0.0
```

Or with `podman` on RHEL / OpenShift:

```bash
podman build -t quay.io/<your-org>/telco-data-generator:1.0.0 .
podman push quay.io/<your-org>/telco-data-generator:1.0.0
```

### 2. Set your OpenShift project

```bash
oc project <your-namespace>
```

### 3. Create the database secret

**Option A — single URL secret (recommended)**

```bash
oc create secret generic data-generator-db \
  --from-literal=DATABASE_URL=postgresql://user:password@postgresql:5432/telco_revenue
```

Then reference it in the Helm release:

```bash
helm install data-generator ./helm/data-generator \
  --set image.repository=quay.io/<your-org>/telco-data-generator \
  --set image.tag=1.0.0 \
  --set database.existingSecret=data-generator-db \
  --set database.existingSecretKey=DATABASE_URL
```

**Option B — let the chart create the secret**

Pass the individual connection fields. The chart builds the URL and stores it
in a new Secret named `<release>-data-generator-db`.

```bash
helm install data-generator ./helm/data-generator \
  --set image.repository=quay.io/<your-org>/telco-data-generator \
  --set image.tag=1.0.0 \
  --set database.host=postgresql \
  --set database.name=telco_revenue \
  --set database.username=telco \
  --set database.password=<your-password>
```

### 4. Run the full dataset (production)

```bash
helm install data-generator ./helm/data-generator \
  --set image.repository=quay.io/<your-org>/telco-data-generator \
  --set image.tag=1.0.0 \
  --set database.existingSecret=data-generator-db \
  --set generator.nCustomers=10000 \
  --set generator.nTransactions=1000000 \
  --set generator.nRegions=4 \
  --set generator.coupling=0.7
```

### 5. Monitor the Job

```bash
# Watch pod status
oc get pods -l app.kubernetes.io/name=data-generator -w

# Stream logs
oc logs -f job/data-generator-data-generator

# Check Job completion
oc get job data-generator-data-generator
```

### 6. Re-run (upgrade)

The Job is idempotent — it drops and recreates both tables on every run.
To trigger a re-run after a Helm upgrade, delete the completed Job first:

```bash
oc delete job data-generator-data-generator
helm upgrade data-generator ./helm/data-generator [--set ...]
```

---

## Parameters reference

### Generator parameters

| Parameter | Default | Description |
|---|---|---|
| `generator.nCustomers` | `10000` | Number of unique customers (0 = all ~27k from HuggingFace) |
| `generator.nTransactions` | `1000000` | Number of transactions (0 = all 1M from HuggingFace) |
| `generator.nRegions` | `4` | Number of geographic regions clustered from Starlink data. Use 4 for North/South/East/West-style demo narrative |
| `generator.coupling` | `0.7` | QoE-churn coupling strength. `0.0` = random assignment, `1.0` = churned customers always land in lowest-QoE region. Higher value makes the agent's cross-model correlation more visible |
| `generator.txStrategy` | `weighted` | Transaction assignment strategy: `weighted` (by avg bill amount) or `uniform` (random) |
| `generator.seed` | `42` | Random seed for reproducibility |
| `generator.snapshotDate` | `2021-06-01` | Snapshot date used for Helm chart parameter compatibility |

### Database parameters

| Parameter | Default | Description |
|---|---|---|
| `database.existingSecret` | `""` | Name of an existing OpenShift Secret containing the database URL. If set, the fields below are ignored |
| `database.existingSecretKey` | `DATABASE_URL` | Key inside the existing secret that holds the PostgreSQL URL |
| `database.host` | `postgresql` | PostgreSQL service hostname (used only when `existingSecret` is empty) |
| `database.port` | `5432` | PostgreSQL port |
| `database.name` | `telco_revenue` | Database name |
| `database.username` | `""` | Database user (used only when `existingSecret` is empty) |
| `database.password` | `""` | Database password (used only when `existingSecret` is empty) |

### Job parameters

| Parameter | Default | Description |
|---|---|---|
| `job.ttlSecondsAfterFinished` | `600` | Seconds before the completed Job pod is automatically deleted. Set to `0` to delete immediately, `-1` to keep indefinitely |
| `job.backoffLimit` | `0` | Number of retries on failure. `0` = fail immediately (recommended for seed jobs) |
| `job.restartPolicy` | `Never` | Pod restart policy |
| `job.serviceAccountName` | `""` | Optional ServiceAccount name if your namespace requires one |

### Image parameters

| Parameter | Default | Description |
|---|---|---|
| `image.repository` | `quay.io/your-org/telco-data-generator` | Container image repository |
| `image.tag` | `1.0.0` | Image tag |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy |

### Resource parameters

| Parameter | Default | Description |
|---|---|---|
| `resources.requests.cpu` | `500m` | CPU request. The job is CPU-bound during K-means clustering |
| `resources.requests.memory` | `2Gi` | Memory request. Full 1M-transaction dataset peaks at ~1.5 Gi |
| `resources.limits.cpu` | `2` | CPU limit |
| `resources.limits.memory` | `4Gi` | Memory limit |

---

## Generated tables

### `customers`

One row per unique customer. Used by the churn model and QoE model.

| Column | Type | Source |
|---|---|---|
| `customer_id` | VARCHAR(20) PK | churn dataset |
| `has_churned` | BOOLEAN | churn dataset (ground truth label) |
| `avg_bill_amount` | FLOAT | mean(TotalBillAmount) across all billing cycles |
| `call_minutes` … `support_channel` | various | 14 churn model input features |
| `region_id` | INTEGER | Starlink cluster assignment |
| `latitude`, `longitude` | FLOAT | Starlink cluster centroid |
| `season`, `weather` | VARCHAR | Starlink cluster mode |
| `qoe_score` | FLOAT | Starlink cluster mean QoE |
| `qoe_download_mbps` | FLOAT | Starlink cluster mean download throughput |
| `qoe_upload_mbps` | FLOAT | Starlink cluster mean upload throughput |
| `qoe_latency_ms` | FLOAT | Derived from packet_loss (35–80 ms range) |

### `transactions`

One row per transaction. Used by the RAFM fraud model.

| Column | Type | Source |
|---|---|---|
| `transaction_id` | BIGSERIAL PK | auto-generated |
| `customer_id` | VARCHAR(20) FK | synthetically assigned from customers table |
| `call_duration` … `avg_cost` | FLOAT | 12 RAFM fraud model input features |
| `plan_type` | VARCHAR(20) | `prepaid` or `postpaid` (encoded to 0/1 at prediction time) |
| `fraud` | SMALLINT | ground truth fraud label (0 or 1) |

---

## Coupling explained

`generator.coupling` controls how strongly poor QoE region correlates with churn:

```
coupling = 0.0   customers assigned to regions randomly — no QoE/churn pattern
coupling = 0.5   partial correlation (default in dry-run tests)
coupling = 0.7   clear correlation — recommended for demo (churned customers
                 cluster in low-QoE regions, enabling the agent to discover:
                 "Region 2 has QoE 34/100 AND 35% churn rate")
coupling = 1.0   fully deterministic — churned → worst QoE, safe → best QoE
```

Higher coupling makes the agent's insight more dramatic and easier to demonstrate.
