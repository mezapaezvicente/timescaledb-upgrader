# timescaledb-upgrader

A chart-agnostic toolkit for upgrading and rolling back **self-hosted
TimescaleDB on Kubernetes** — plus a local test harness to rehearse the whole
thing before you touch a cluster that matters.

Built for people running TimescaleDB on their own clusters, often on the
[archived Timescale Helm charts](https://github.com/timescale/helm-charts) or a
fork of them. Nothing here depends on those charts. The tooling talks to a live
cluster and a live database, so it works the same whether your StatefulSet came
from a chart, a fork, Kustomize, or hand-written YAML.

> **Status:** Phases 1–3 are complete. The upgrade and rollback paths
> themselves are not implemented yet — see [Roadmap](#roadmap).

---

## Why this exists

Upgrading TimescaleDB means moving two versions at once — PostgreSQL and the
TimescaleDB extension — and the safe path between any two points depends on
both. Get it wrong and the failure mode is a database that will not start, on a
volume you cannot easily roll back.

The parts that make that survivable are unglamorous: knowing exactly what
versions you are running right now, knowing whether the target image exists,
knowing whether you have the disk headroom for a dump, and having rehearsed the
procedure somewhere it does not matter. That is what this repo is about.

**A note on the version matrix.** Nothing in this repo asserts a compatibility
fact that has not been verified against a source. Entries that have not been
verified are marked `"verified": false` with a null source rather than filled in
from memory. An upgrade tool that guesses is worse than no tool at all.

---

## Requirements

| Tool | Why |
|---|---|
| [Docker](https://docs.docker.com/get-docker/) | runs the kind node |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) ≥ 0.20 | the local cluster |
| `kubectl` | everything |
| `make`, `bash`, `sed` | the entry points |

On Windows, run from **Git Bash** or **WSL**. The scripts pipe manifests to
`kubectl` over stdin specifically so they work there.

The first `make up` downloads the TimescaleDB HA image, which is several GB.
Later runs reuse it.

---

## Quickstart

```bash
make up      # create the cluster and bring up TimescaleDB under Patroni
make smoke   # prove it works: connect, create a hypertable, insert, query
make down    # destroy everything
```

`make up` is idempotent — re-run it to apply manifest changes or to change the
replica count.

<details>
<summary>What <code>make up</code> actually does</summary>

1. Creates a single-node kind cluster from [`cluster/kind-config.yaml`](cluster/kind-config.yaml),
   with the node image pinned by digest.
2. Renders [`deploy/*.yaml`](deploy/) and applies them: namespace, RBAC, a
   credentials Secret, the Patroni ConfigMap, three Services, and the
   StatefulSet.
3. Waits for every Patroni member to report ready over its REST API.
4. Creates the application database and `CREATE EXTENSION timescaledb`.

</details>

### Seeding data

```bash
make seed                                            # 100k rows
make seed ROWS=50000000 CHUNK_INTERVAL="6 hours"     # something to time
```

Values are **deterministic**, not random: seeding the same parameters twice
produces identical data, which is what lets you compare a database before and
after an upgrade and claim the contents are unchanged. The table is recreated on
each run, so `ROWS` is exact.

| Knob | Default | Meaning |
|---|---|---|
| `ROWS` | `100000` | total rows |
| `CHUNK_INTERVAL` | `1 day` | hypertable `chunk_time_interval` |
| `DEVICES` | `10` | distinct `device_id` values |
| `STEP` | `10 seconds` | time between ticks |
| `START_TS` | `2024-01-01 00:00:00+00` | first timestamp |
| `TABLE` | `metrics` | table name |

### Other targets

```bash
make status       # Patroni members, pods, services, volumes
make psql         # interactive shell on the current primary
make logs         # tail the primary
make clean        # drop the namespace, keep the cluster
make load-image   # side-load the image from local Docker instead of pulling
```

`make load-image` uses `kind load docker-image`. That fails for the
multi-platform `timescaledb-ha` images when Docker uses the containerd image
store, which is the Docker Desktop default. The error is
`ctr: content digest sha256:... not found`
([kind#3795](https://github.com/kubernetes-sigs/kind/issues/3795), unfixed as
of kind v0.33.0). Until the script is changed, load a single platform by hand:

```bash
docker save --platform linux/amd64 "$TSDB_IMAGE" \
  | docker exec -i "${KIND_CLUSTER}-control-plane" \
      ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs -
```

kind's own documented alternative is `docker image save --platform linux/amd64
-o image.tar "$TSDB_IMAGE"`, then `kind load image-archive image.tar --name
"$KIND_CLUSTER"`. It works too, but writes a multi-GB temp file. The
verification harness already loads images the first way.

### Knobs

Override any of these on the command line:

```bash
make up REPLICAS=3
make up TSDB_IMAGE=timescale/timescaledb-ha:pg17.11-ts2.30.1
make up NAMESPACE=timescale-test CLUSTER_NAME=tsdb2
```

| Knob | Default |
|---|---|
| `TSDB_IMAGE` | `timescale/timescaledb-ha:pg16.15-ts2.30.1` |
| `REPLICAS` | `1` |
| `NAMESPACE` | `timescale` |
| `CLUSTER_NAME` | `tsdb` |
| `STORAGE` | `5Gi` |
| `APP_DB` | `bench` |

---

## How the harness is built

**Own manifests, no Helm.** The Timescale charts are archived, so depending on
them would build this repo on abandoned tooling. [`deploy/`](deploy/) holds
plain YAML with `__PLACEHOLDER__` tokens that `scripts/lib.sh` substitutes and
pipes into `kubectl apply -f -`.

**Patroni uses the Kubernetes API as its DCS.** No etcd, no Consul — Patroni
stores the leader lock in an `Endpoints` object and needs only a namespaced
`Role`. See [`deploy/10-rbac.yaml`](deploy/10-rbac.yaml).

**Three Services, three jobs.** `tsdb` is selector-less and Patroni writes its
endpoints, so it always points at the current leader; `tsdb-headless` gives
StatefulSet pods stable DNS; `tsdb-replica` selects on the `role=replica` label
Patroni maintains. Those label *values* are pinned in the Patroni config rather
than inherited, because the defaults have changed across Patroni majors.

**`updateStrategy: OnDelete`.** A database StatefulSet should not be rolled by a
controller unsupervised — least of all one whose pod template is about to change
PostgreSQL major versions. Pod replacement is a deliberate act here.

**The app database is created after bootstrap, not in Patroni's
`post_bootstrap` hook.** A failing bootstrap hook makes Patroni wipe the data
directory and retry, which turns a one-line SQL mistake into an opaque crash
loop.

---

## The version matrix

Upgrade compatibility lives in [`data/upgrade-matrix.json`](data/upgrade-matrix.json)
as **data, not code**. Adding a supported path means editing JSON and citing a
source; it does not mean editing Python. The published table is
[`docs/matrix.md`](docs/matrix.md).

Each row records a transition and what is known about it:

```json
{
  "source_pg": "15", "source_ts": "2.28",
  "target_pg": "15", "target_ts": "2.30",
  "verdict": "no_image",
  "reason": "No timescale/timescaledb-ha tag pairs PostgreSQL 15 with TimescaleDB 2.30. ...",
  "steps": [],
  "verified": true,
  "evidence": null,
  "references": ["https://registry-1.docker.io/v2/timescale/timescaledb-ha/tags/list?n=10000"]
}
```

| Verdict | Meaning |
|---|---|
| `in_place` | The data directory can be upgraded without exporting the data. |
| `dump_restore` | The data must be exported and reloaded. |
| `unsupported` | Not a supported path. |
| `no_image` | No published image exists for the target pair. |
| `null` | **Not established.** The tooling refuses to act on it. |

### What is verified, and what is not

The loader enforces the rule that keeps this honest: **a row marked `verified`
must carry a verdict and the `evidence` behind it** — the records of running
that migration with this tool (`verify/harness.py`), once on an empty database
and once on a seeded one, and for a PostgreSQL major change both again with
`pg_upgrade --link`. A row that is not verified must be blank: no verdict, no
reason, no evidence. `no_image` is the one exception, because the container
registry answers it: such a row cites the registry listing in `references`.
Otherwise `references` holds upstream issues and release notes that explain a
verdict; they are never its proof.

Today 110 of the 131 rows are verified: every same-PG row, and the seven rows
that change major from pg11 2.1 and pg12 2.11. The other 21 cross-PG rows ship
as `null` / `verified: false`.

### Which images actually exist

[`data/images.json`](data/images.json) is generated by querying the Docker
registry, not written by hand:

```bash
make images           # summarise the catalog
make images-refresh   # re-query the registry and regenerate it
```

### What each image really installs

An image's registry labels predict the TimescaleDB versions it ships, and the
prediction is wrong in ways that strand databases: `pg15.18-ts2.28.3` is
labelled from 2.12.2 but cannot install below 2.13.0, `pg12.22-ts2.11.2` ships
1.7.5 and then 2.1.0 onwards with no 2.0 at all, and the oldest images carry no
version labels. [`data/image-contents.json`](data/image-contents.json) records
what each image this repository uses actually installs, measured with `ls` in a
throwaway container (install script and shared object both present). `drift`
and `plan` use a measured entry in place of the labels.

```bash
make image-contents                       # re-measure (images must be pulled)
bash scripts/tsupgrade.sh contents show   # one line per image, holes visible
```

### Looking up a transition

```bash
make lookup FROM_PG=15.18 FROM_TS=2.28.3 TO_PG=15 TO_TS=2.30
```

Versions are matched at their natural unit — PostgreSQL major, TimescaleDB
series — so a discovered `15.18` / `2.28.3` finds the row written as `15` /
`2.28`. A row may pin an exact patch release, and then it must match exactly and
takes precedence over the series-wide row around it.

Exit codes are meant to be scripted against:

| Code | Meaning |
|---|---|
| `0` | a verdict, verified and sourced |
| `3` | a row exists but its verdict is not established |
| `4` | no row at all — the matrix says nothing about this transition |
| `1` | error |

Codes `3` and `4` are deliberately different. "We have not established this" is
not the same answer as "there is no such transition", and neither is permission
to proceed.

### Regenerating

```bash
make matrix-scaffold   # add transitions the catalog implies; existing rows are kept
make matrix            # render docs/matrix.md
make test              # unit tests over the loader, lookup and shipped data
```

`matrix-scaffold` never overwrites an existing row, so refreshing the catalog
after a new TimescaleDB release adds the newly possible transitions and leaves
verified work alone.

---

## Preflight

One command, run before you touch anything:

```bash
make preflight TARGET_IMAGE=timescale/timescaledb-ha:pg17.11-ts2.30.1
```

It answers, for the cluster you are actually pointed at:

| Check | What it establishes |
|---|---|
| Cluster and namespace | Echoes back the context, API server, Kubernetes version and namespace, so you can confirm you are where you think you are. |
| Target image exists | Queries the target's registry for the manifest. |
| Target image on nodes | Whether the image is already cached on each node. |
| Running versions, per instance | PostgreSQL and TimescaleDB versions **queried from each database**, per member, and per database. |
| Disk headroom | Free space where a dump would land, against the live database size. |
| Permissions | Ownership, mode and writability of the data directory and dump path, plus PVC capacity, access modes and whether the storage class can expand. |
| Matrix verdict | Looks the discovered versions up against the requested target. |

Versions come from `current_setting('server_version')` and `pg_extension` — never
from the image tag, and never from chart values. A tag records what someone
*intended* to deploy; only the database knows what is running. The extension
version is read **per database**, because they can legitimately differ, and a
disagreement is reported rather than flattened into one confident answer.

### It is read-only, and that is enforced

- Only `kubectl get` / `exec` are used. Never `apply`, `patch`, `delete` or `scale`.
- Every connection opens with `PGOPTIONS=-c default_transaction_read_only=on`,
  so **PostgreSQL itself** rejects any statement that would write, whatever the
  SQL says.
- Commands run inside pods are limited to `df`, `stat`, `id` and `test`.

This is why "is the image pullable from this cluster?" reports `unknown` rather
than `pass` when the image is not already cached: proving it would require
pulling, which writes to node storage. The check reports what it can observe and
declines to imply more.

### pass, fail, unknown

`unknown` is a real answer and is never quietly promoted to `pass`. A check that
could not be carried out has not been passed — and a matrix row whose verdict
nobody has established is `unknown`, not approval.

| Exit | Meaning |
|---|---|
| `0` | every check passed |
| `2` | at least one check failed |
| `3` | at least one check is unknown, none failed |
| `1` | preflight could not run |

Registry auth failures and timeouts are **failures**, not unknowns, and every
request carries an explicit timeout so nothing hangs a maintenance window. A
registry that will not answer is never recorded as a registry that has no such
image.

---

## Plan and apply

`plan` decides and writes nothing; `apply` executes a plan and is the only
command that writes. They are separate so a plan can be read, diffed and
approved before anything touches a cluster.

```bash
bash scripts/tsupgrade.sh plan --from-pg 15.13 --from-ts 2.12.2   --target-image timescale/timescaledb-ha:pg15.18-ts2.28.3 --output plan.json
bash scripts/tsupgrade.sh apply plan.json --report report.json
```

**A verified row is verified for its route.** Its records ran one route
(which image each update and each `pg_upgrade` runs in), and the matrix row
names it, every image pinned by digest. `plan` builds the row from exactly
those images and calls the result verified only when it is that route again;
anything else is unestablished. A cluster on an older patch of the source
series first updates to the release the route was recorded from, and the
plan says so. Every image in a plan carries its digest, and `apply` rolls
out `image@sha256:...`, so a tag pushed again later cannot change what runs.
`--via` offers other images, for `--verification-run` only.

A plan is a list of hops: one image the cluster must run, then one `ALTER
EXTENSION timescaledb UPDATE TO '<v>'`. Each hop runs preflight against its
image (any FAIL stops), rolls the StatefulSet onto it if needed (replicas
first, primary last), updates every database in a fresh `psql -X` session,
cycles client sessions so none keeps the old library loaded, and confirms
every database on every instance reports the new version.

`apply` refuses, having written nothing, when the cluster is not in the state
the plan was made from, when an update step carries SQL other than the one
statement its version implies, and when the plan was built with
`--verification-run` unless `--acknowledge-verification-run` is also given.
It stops at the first failing step, keeps the error verbatim, and reports what
it had already done. It never retries or rolls back on its own.

`--database` scopes a run to named databases; without it every database with
TimescaleDB must be at the source version, because a real upgrade has to
cover them all.

### Changing the PostgreSQL major

A target on a newer major is planned as a route of `pg_upgrade` hops between
same-major segments, from the measured contents of the route's images (an
`-all` tag carries several majors' binaries, but not every major: none pairs
pg12 with anything past pg13). Each `pg_upgrade` runs inside an
image carrying both majors, at a TimescaleDB release both sides ship, so the
old major is updated to that release first. The route takes the fewest
`pg_upgrade` runs, and the same-major segments keep their stops: pg12 2.11 to
pg16 runs 12 → 13, 13 → 15 and 15 → 16, and stops at 2.27.2 on pg15 on the way.

```bash
bash scripts/tsupgrade.sh plan --from-pg 12.22 --from-ts 2.11.2 \
  --target-image timescale/timescaledb-ha:pg16.15-ts2.30.1 --output plan.json
```

Under Patroni, `apply` moves the leader to ordinal 0, scales the StatefulSet
to 0, runs `pg_upgrade` in a pod on ordinal 0's disk (matching the old
cluster's data checksums), deletes the replicas' disks and Patroni's DCS
state (`pg_upgrade` gives the cluster a new system identifier), and restarts
everything on the new major's image; Patroni re-clones the replicas. The plan
ends with `vacuumdb --all --analyze-in-stages`. `apply` waits for `pg_upgrade` as long as
it runs (`--pg-upgrade-timeout` sets a limit; a pod that never starts fails
after `--rollout-timeout`). When it stops waiting, the pod keeps running and
the report says how to watch it and the commands that finish the upgrade. An
upgrade pod already there is refused, never deleted. Every failure after the
cluster is stopped says what state it left and how to go on. After a failed
`--link`, that includes whether linking had begun: if it had, `pg_upgrade`
renamed the old cluster's `global/pg_control` to `pg_control.old`, and the
report says to rename it back before starting the old major (the
documented recovery; the new cluster is never started by then). By default `pg_upgrade`
copies, and the old data directory stays beside the new one to go back to;
`plan --link` hard-links instead, which is fast and needs no space but leaves
the old directory unusable. A failure before anything is deleted leaves the
cluster stopped with its data intact, and says how to start it again.

`pg_upgrade` may write an `update_extensions.sql` holding a bare
`ALTER EXTENSION timescaledb UPDATE`. `apply` never runs it, and removes it:
run on the new cluster, it goes straight to that image's default release,
past any stop the plan makes (after 13 → 15, from 2.15.3 to 2.28.3, past the
2.27.2 stop). Both modes are verified on kind
for the seven rows from pg11 2.1 and pg12 2.11, and the records show what
`pg_upgrade` did: copied files, or hard links into the old directory.

---

## Layout

```
cluster/           kind cluster definition (node image pinned by digest)
deploy/            Kubernetes manifests with __PLACEHOLDER__ tokens
scripts/           the real logic; the Makefile is just a table of contents
  lib.sh           shared config, rendering, psql/kubectl helpers
data/
  images.json      generated: which image tags the registry actually serves
  image-contents.json   generated: what specific images can really install
  upgrade-matrix.json   the compatibility matrix
docs/matrix.md     generated: the published version table
tools/tsupgrade/   the CLI (`python -m tsupgrade`), standard library only
  preflight.py     the read-only pre-upgrade checks
  plan.py          builds the runbook; writes nothing
  majors.py        routes across PostgreSQL majors (pg_upgrade hops)
  apply.py         executes a plan; the only module that writes
verify/            the seeded fixture and verification-run evidence
tests/             unit tests over the loader, lookup and shipped data
```

---

## Roadmap

- [x] **Phase 1** — local test harness: `make up`, `make down`, seeding, smoke test
- [x] **Phase 2** — version matrix as data, with a loader, lookup and published table
- [x] **Phase 3** — read-only preflight checks against a live instance
- [x] **Phase 4** — drift classifier, update-script gates, measured image contents
- [x] **Phase 5** — `plan` and `apply` (extension updates and image rollouts)
- [ ] Verification harness and a matrix established by running every row
- [ ] Rollback execution
- [ ] Maintenance-window estimation

---

## License

[MIT](LICENSE)
