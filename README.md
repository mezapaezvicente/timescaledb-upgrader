# timescaledb-upgrader

A chart-agnostic toolkit for upgrading and rolling back **self-hosted
TimescaleDB on Kubernetes** — plus a local test harness to rehearse the whole
thing before you touch a cluster that matters.

Built for people running TimescaleDB on their own clusters, often on the
[archived Timescale Helm charts](https://github.com/timescale/helm-charts) or a
fork of them. Nothing here depends on those charts. The tooling talks to a live
cluster and a live database, so it works the same whether your StatefulSet came
from a chart, a fork, Kustomize, or hand-written YAML.

> **Status:** Phases 1 and 2 are complete. The upgrade and rollback paths
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
  "source_url": "https://registry-1.docker.io/v2/timescale/timescaledb-ha/tags/list?n=10000",
  "verified": true
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
must carry both a verdict and a `source_url`**, and a row that is not verified
must be blank — no verdict, no reason, no source. There is no state in which the
matrix asserts something without saying where it came from.

Today, the only verdicts filled in automatically are `no_image`, because that
one is a question about a container registry and the registry answers it.
Whether a given transition works in place or needs a dump and restore is a
compatibility fact, and those rows ship as `null` / `verified: false` for a
human to establish and cite.

### Which images actually exist

[`data/images.json`](data/images.json) is generated by querying the Docker
registry, not written by hand:

```bash
make images           # summarise the catalog
make images-refresh   # re-query the registry and regenerate it
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
make test              # 102 unit tests over the loader, lookup and shipped data
```

`matrix-scaffold` never overwrites an existing row, so refreshing the catalog
after a new TimescaleDB release adds the newly possible transitions and leaves
verified work alone.

---

## Layout

```
cluster/           kind cluster definition (node image pinned by digest)
deploy/            Kubernetes manifests with __PLACEHOLDER__ tokens
scripts/           the real logic; the Makefile is just a table of contents
  lib.sh           shared config, rendering, psql/kubectl helpers
data/
  images.json      generated: which image tags the registry actually serves
  upgrade-matrix.json   the compatibility matrix
docs/matrix.md     generated: the published version table
tools/tsupgrade/   the CLI (`python -m tsupgrade`), standard library only
tests/             unit tests over the loader, lookup and shipped data
```

---

## Roadmap

- [x] **Phase 1** — local test harness: `make up`, `make down`, seeding, smoke test
- [x] **Phase 2** — version matrix as data, with a loader, lookup and published table
- [ ] **Phase 3** — read-only preflight checks against a live instance
- [ ] Upgrade and rollback execution
- [ ] Maintenance-window estimation

---

## License

[MIT](LICENSE)
