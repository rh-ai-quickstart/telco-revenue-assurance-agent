# Read-only model storage on OpenShift

The optional [training infrastructure](../training/README.md) owns S4 storage and
the training write credentials. Model serving uses a separate S4 user,
`telco-model-serving`, with `op-mask=read`, no administration capabilities and one
S3 key. An additive policy grants bucket listing and object reads only under
`mybucket/models/`. It grants no writes or deletes. Existing bucket policy
statements and the training owner's keys and permissions are preserved.

From the quickstart repository, with an active OpenShift login:

```bash
make prepare-model-storage NAMESPACE=telco-revenue-assurance-agent
```

The helper uses a loopback-only port-forward to the private S4 Service. It creates
a disposable object under `models/_telco-serving-access-check/`, verifies that the
serving identity can list and read it, and requires `AccessDenied` with HTTP 403
when that identity tries to overwrite or delete it. It verifies the object is
unchanged, then removes it using the owner credentials. A different error, such
as `InvalidAccessKeyId`, does not count as a successful permission check.

The target runs `deployment/models/prepare_storage.py` with `boto3==1.43.119`
and saves nonsecret evidence to `.qs-factory/pipeline/model-storage-permissions.json`.
Set `MODEL_STORAGE_REPORT` to choose a different report path.

Only after those checks pass does it create these connection Secrets:

- `telco-churn-model-storage`
- `telco-fraud-model-storage`
- `telco-qoe-model-storage`

They contain the same read-only serving credential and S3 connection settings,
with the OpenShift AI labels and annotation required for connection discovery.
Credentials stay in memory and are sent to `oc` through stdin; the report records
only names, operation results and nonsecret configuration. Existing matching
identities, policy statements and Secrets are reused. Conflicting Secrets,
permissions or policy statements stop preparation; the helper never rotates
credentials silently. A newly created identity that fails verification is
removed when it has not yet been referenced by a connection Secret, and its
added bucket policy is rolled back if no concurrent policy change occurred.

The S4 DBStore backend indexes one S3 access key per user. Do not attach a second
serving key or subuser key to the training owner: a distinct user avoids that
backend limitation. The policy allows listing the bucket's keys, while object
contents are readable only below `models/`; it does not isolate the three model
families from each other. The same storage PVC persists both model objects and
the serving user metadata. The connection Secrets live outside Helm so an
application upgrade does not rotate them.

These checks establish storage permissions. Model files, preprocessing artifacts,
inference authentication and real predictions are verified separately; see
[serving configuration](../serving/README.md).

Local safety checks:

```bash
uv run --frozen --with boto3==1.43.119 pytest -q deployment/models/tests/
uv run --frozen ruff check deployment/models/
uv run --frozen ruff format --check deployment/models/
```

The approach follows the [Ceph user administration guide](https://docs.ceph.com/en/latest/radosgw/admin/)
and [bucket policy documentation](https://docs.ceph.com/en/latest/radosgw/bucketpolicy/).
