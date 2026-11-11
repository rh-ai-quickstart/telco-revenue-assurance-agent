# PostgreSQL in `telco-revenue-assurance-agent`

This guide covers the PostgreSQL instance installed with the `pgvector` Helm chart in the ptnr2 OpenShift cluster. The release is `postgres` in namespace `telco-revenue-assurance-agent`. It runs the official PostgreSQL 17 image with a 5 GiB persistent volume. The pgvector extension is not installed or available.

The in-cluster service is `postgres.telco-revenue-assurance-agent.svc.cluster.local:5432`. The default database and user are both `telco-revenue-assurance-agent` and `postgres`, respectively. The password is stored in the Kubernetes Secret `postgres` in namespace `telco-revenue-assurance-agent`, under key `password`. The other Secret keys are `user`, `dbname`, `host`, `port`, `uri`, and `jdbc-uri`.

## Connect from a pod

An application pod in `telco-revenue-assurance-agent` can reach PostgreSQL at `postgres:5432`. Pods in another namespace should use the full service name, `postgres.telco-revenue-assurance-agent.svc.cluster.local`. Kubernetes Secrets are namespace-scoped: an application in a different namespace needs its own Secret there, created through your approved secret management process.

The chart Secret contains the PostgreSQL administrator account. It can be wired into a short-lived admin client or a smoke-test pod with the following environment variables, but use a dedicated database role for a running application:

```yaml
env:
  - name: PGHOST
    valueFrom:
      secretKeyRef:
        name: postgres
        key: host
  - name: PGPORT
    valueFrom:
      secretKeyRef:
        name: postgres
        key: port
  - name: PGDATABASE
    valueFrom:
      secretKeyRef:
        name: postgres
        key: dbname
  - name: PGUSER
    valueFrom:
      secretKeyRef:
        name: postgres
        key: user
  - name: PGPASSWORD
    valueFrom:
      secretKeyRef:
        name: postgres
        key: password
```

These are the standard libpq environment variables, so `psql`, `pg_dump`, and many PostgreSQL drivers use them automatically. For example, an application using Python and Psycopg can connect with:

```python
import os
import psycopg

with psycopg.connect(
    host=os.environ["PGHOST"],
    port=os.environ["PGPORT"],
    dbname=os.environ["PGDATABASE"],
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user, version()")
        print(cur.fetchone())
```

Install the driver in a Python environment with `pip install "psycopg[binary]"`. The application environment above should use its own database Secret and role as described below. Keep credentials in a Kubernetes Secret or your approved secret manager; do not commit them to application source, values files, or container images.

### Create application credentials

Connect as the database administrator, then create a login role and an application-owned database. `\password` is a `psql` command that prompts for the password without putting it in SQL history:

```text
CREATE ROLE revenue_app LOGIN;
\password revenue_app
CREATE DATABASE revenue_assurance OWNER revenue_app;
```

Create a Secret for the application in its namespace, using your approved secret management process. Give it these keys so the Python example and other libpq clients can consume them directly:

| Secret key | Value |
| --- | --- |
| `PGHOST` | `postgres` in `telco-revenue-assurance-agent`, or `postgres.telco-revenue-assurance-agent.svc.cluster.local` from another namespace |
| `PGPORT` | `5432` |
| `PGDATABASE` | `revenue_assurance` |
| `PGUSER` | `revenue_app` |
| `PGPASSWORD` | The password entered with `\password` |

In the Deployment, load those keys with `envFrom`:

```yaml
envFrom:
  - secretRef:
      name: revenue-app-db
```

If the application is outside `telco-revenue-assurance-agent`, cluster DNS resolves the service name, subject to the cluster's network policies. The application Secret must still exist in the application's own namespace.

## Connect with `psql` inside the cluster

Run this from the jumphost, where the kubeconfig is located. An administrator with permission to exec into the database pod can use its local PostgreSQL socket:

```sh
oc --kubeconfig=/root/kubeconfigs/ptnr2/kubeconfig \
  exec -it -n telco-revenue-assurance-agent postgres-0 -- \
  psql -U postgres -d telco-revenue-assurance-agent
```

Useful commands in the `psql` prompt:

```text
\conninfo
\dt
\l
SELECT current_database(), current_user, now();
\q
```

## Connect from a workstation

The chart creates an internal `ClusterIP` Service, with no external access configured by the chart. Use a port forward to connect from a workstation. On a machine with the kubeconfig, run:

```sh
oc --kubeconfig=/root/kubeconfigs/ptnr2/kubeconfig \
  port-forward -n telco-revenue-assurance-agent svc/postgres 15432:5432
```

If the kubeconfig is only available on the jumphost, start that command there, then open an SSH tunnel from your workstation in a second terminal:

```sh
ssh -N -L 15432:127.0.0.1:15432 root@192.168.27.13
```

Connect your local PostgreSQL client to `localhost:15432`, database `telco-revenue-assurance-agent`, user `postgres`, and provide the password from Secret `telco-revenue-assurance-agent/postgres`. For example, with `psql`:

```sh
psql "host=localhost port=15432 dbname=telco-revenue-assurance-agent user=postgres sslmode=prefer" -W
```

`-W` prompts for the password so it does not need to appear in shell history or the connection string.

## Check the deployment

Run these commands from a host with `oc` configured for ptnr2:

```sh
oc get pods,pvc,svc -n telco-revenue-assurance-agent -l app.kubernetes.io/instance=postgres
helm status postgres -n telco-revenue-assurance-agent
oc logs -n telco-revenue-assurance-agent postgres-0
```

The expected pod is `postgres-0` and the persistent volume claim is `pg-data-postgres-0`. The PVC keeps the database files when the pod restarts. Back up important data separately; the PVC is not a backup.

To confirm PostgreSQL has no vector extension, connect with `psql` and run:

```sql
SELECT name FROM pg_available_extensions WHERE name = 'vector';
SELECT extname FROM pg_extension WHERE extname = 'vector';
```

Both queries should return no rows. This image does not include pgvector, so `CREATE EXTENSION vector;` will fail.

## Helm maintenance

The installed chart and its deployment files are on the jumphost in `/root/deployments/telco-revenue-assurance-agent-postgres.TwpRI4/`. The checked-in values file is [`values-telco-revenue-assurance-agent.yaml`](values-telco-revenue-assurance-agent.yaml); it pins chart source commit `579379152f9dcd915d5b12292f06ea74cb71d3b2` and the PostgreSQL image digest.

The values file intentionally leaves `secret.password` empty. On an upgrade, reuse the password from the live Secret so Helm does not replace it with an empty value. Run this on the jumphost:

```sh
export KUBECONFIG=/root/kubeconfigs/ptnr2/kubeconfig
cd /root/deployments/telco-revenue-assurance-agent-postgres.TwpRI4
umask 077
password_file=$(mktemp)
trap 'rm -f "$password_file"' EXIT
oc get secret postgres -n telco-revenue-assurance-agent \
  -o jsonpath='{.data.password}' | base64 --decode > "$password_file"
helm upgrade postgres ./helm -n telco-revenue-assurance-agent \
  -f values-telco-revenue-assurance-agent.yaml \
  --set-file "secret.password=$password_file" \
  --atomic --wait --timeout 10m
```

The temporary file is readable only by the current user and is removed when the shell exits. Changing the Kubernetes Secret alone does not change the password for the PostgreSQL role in the existing database. Password rotation requires changing both the Secret and the PostgreSQL role password together.

Do not uninstall the Helm release as a way to restart PostgreSQL. The database state lives on the PVC; take a database backup before changing or removing storage.
