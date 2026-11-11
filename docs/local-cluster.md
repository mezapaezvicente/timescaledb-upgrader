# The local test cluster

A single-node kind cluster running TimescaleDB under Patroni, built from this
repository's own manifests. Use it to try the tooling, or to rehearse a
procedure somewhere it does not matter. The verification harness
([`verify/README.md`](../verify/README.md)) runs on clusters of the same shape.

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

1. Creates a single-node kind cluster from [`cluster/kind-config.yaml`](../cluster/kind-config.yaml),
   with the node image pinned by digest.
2. Renders [`deploy/*.yaml`](../deploy/) and applies them: namespace, RBAC, a
   credentials Secret, the Patroni ConfigMap, three Services, and the
   StatefulSet.
3. Waits for every Patroni member to report ready over its REST API.
4. Creates the application database and `CREATE EXTENSION timescaledb`.

</details>

## Seeding data

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

## Other targets

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

## Knobs

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

## How it is built

**Own manifests, no Helm.** The Timescale charts are archived, so depending on
them would build this repo on abandoned tooling. [`deploy/`](../deploy/) holds
plain YAML with `__PLACEHOLDER__` tokens that `scripts/lib.sh` substitutes and
pipes into `kubectl apply -f -`.

**Patroni uses the Kubernetes API as its DCS.** No etcd, no Consul — Patroni
stores the leader lock in an `Endpoints` object and needs only a namespaced
`Role`. See [`deploy/10-rbac.yaml`](../deploy/10-rbac.yaml).

**Three Services, three jobs.** `tsdb` is selector-less and Patroni writes its
endpoints, so it always points at the current leader; `tsdb-headless` gives
StatefulSet pods stable DNS; `tsdb-replica` selects on the `role=replica` label
Patroni maintains. Those label *values* are pinned in the Patroni config rather
than inherited, because the defaults have changed across Patroni majors.

**`updateStrategy: OnDelete`.** A database StatefulSet should not be rolled by a
controller unsupervised — least of all one whose pod template is about to change
PostgreSQL major versions. Pod replacement is a deliberate act here, and
`apply` refuses a StatefulSet that updates any other way.

**The app database is created after bootstrap, not in Patroni's
`post_bootstrap` hook.** A failing bootstrap hook makes Patroni wipe the data
directory and retry, which turns a one-line SQL mistake into an opaque crash
loop.

**The credentials are throwaway.** [`deploy/20-secret.yaml`](../deploy/20-secret.yaml)
commits fixed passwords on purpose, so `make up` needs no setup. It is not a
template for a real cluster.
