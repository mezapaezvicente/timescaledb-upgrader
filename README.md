# timescaledb-upgrader

[![test](https://github.com/mezapaezvicente/timescaledb-upgrader/actions/workflows/test.yml/badge.svg)](https://github.com/mezapaezvicente/timescaledb-upgrader/actions/workflows/test.yml)

Upgrade and roll back **self-hosted TimescaleDB on Kubernetes under Patroni**:
the extension, the image, and the PostgreSQL major with `pg_upgrade`.

- **`preflight`** reads what is actually running, per instance and per
  database, and checks the target image, disk, other extensions, collations
  and version drift. It is read-only, and PostgreSQL enforces that.
- **`plan`** picks a route through intermediate images and releases, pinned to
  the route its matrix row was verified on, as a runbook or a script you run
  yourself.
- **`apply`** and **`rollback`** carry it out on a Patroni StatefulSet. They
  refuse before changing anything if a check does not hold.
- Python 3.8+, standard library only, plus `kubectl` and `bash`. Every route
  in the matrix was run on a local kind cluster, and `make up` gives you one to
  rehearse on.

Built for people running TimescaleDB on their own clusters, often on the
[archived Timescale Helm charts](https://github.com/timescale/helm-charts) or a
fork of them. Nothing here depends on those charts. The tooling talks to a live
cluster and a live database, not to chart values.

**Reading this as a work sample?** The thinking is in three places:
[Why this exists](#why-this-exists) and its note on the matrix;
[Known limits](#known-limits), which separates what was measured from what was
inferred; and the test names, which state the rules
(`test_rollback_refuses_what_it_cannot_undo`,
`test_every_verified_row_agrees_with_its_evidence`). Every matrix verdict rests
on run records committed under [`verify/evidence/`](verify/evidence/), and a
test fails when a verdict and its records disagree.

> **Status:** preflight, `plan`, `apply` and `rollback` are complete: extension
> updates, image rollouts and PostgreSQL major upgrades with `pg_upgrade`. At
> `v0.2.1` all 131 rows of the matrix are established, 126 by running them with
> this tool. `apply` drives a
> Patroni StatefulSet laid out like the `timescaledb-ha` image's defaults; the
> archived chart's own layout is not supported yet — see
> [Who can use it today](#who-can-use-it-today).

---

## Why this exists

Upgrading TimescaleDB means moving two versions at once — PostgreSQL and the
TimescaleDB extension — and the safe path between any two points depends on
both. Get it wrong and the failure mode is a database that will not start, on a
volume you cannot easily roll back.

The parts that make that survivable are unglamorous: knowing exactly what
versions you are running right now, knowing whether the target image exists,
knowing whether you have the disk headroom, and having rehearsed the procedure
somewhere it does not matter. That is what this repo is about.

**A note on the version matrix.** Nothing in this repo asserts a compatibility
fact it has not established. A transition nobody has run is recorded as
`verdict: null`, `verified: false`, with no evidence, rather than filled in
from memory or from documentation. An upgrade tool that guesses is worse than
no tool at all.

---

## Contents

- [Who can use it today](#who-can-use-it-today)
- [Requirements](#requirements)
- [1. Preflight](#1-preflight)
- [2. Plan](#2-plan)
- [3. Apply](#3-apply)
- [Changing the PostgreSQL major](#changing-the-postgresql-major)
- [Going back from a copy](#going-back-from-a-copy)
- [Without `apply`: a script for any deployment](#without-apply-a-script-for-any-deployment)
- [Where images come from](#where-images-come-from)
- [Old continuous aggregates](#old-continuous-aggregates)
- [The version matrix](#the-version-matrix)
- [Known limits](#known-limits)
- [Try it locally](#try-it-locally)
- [Layout](#layout) · [Roadmap](#roadmap)

---

## Who can use it today

| Your deployment | `preflight`, `plan` | `plan --format script` | `apply`, `rollback` |
|---|---|---|---|
| A Patroni StatefulSet on a `timescaledb-ha` image, `OnDelete`, data on one volume claim (as in [`deploy/`](deploy/)) | yes | yes | yes |
| The archived `timescaledb-single` chart | yes | yes | **not yet** (below) |
| An operator (CloudNativePG, StackGres, Zalando…) | `plan`; preflight not tried | yes | no: the operator would fight `apply` for the pods |

`preflight`, `apply` and `rollback` take `--selector`, `--statefulset` and
`--container`. Without them they use `application=patroni`, `tsdb` and
`timescaledb`, and when that selector finds no pod, they read the names from
Patroni's own pod spec: the pods whose container sets `PATRONI_SCOPE`, the
selector from `PATRONI_KUBERNETES_LABELS`, the StatefulSet that owns them, and
that container. More than one Patroni cluster in the namespace is refused,
with the list. Preflight's "Cluster and namespace" check says what it found.

**The archived chart.** Read from the `timescaledb-single` StatefulSet template
and `values.yaml` on the archived repository's `main` branch; not run against
this tool:

- the template hard-codes `updateStrategy: RollingUpdate`, so `apply` refuses
  the StatefulSet, and patching it to `OnDelete` is undone by the next
  `helm upgrade`;
- the data directory is `/var/lib/postgresql/data`. `apply` now reads it
  from the servers and `rollback` from the report (`--data-dir` overrides
  both); measured in a container, not yet on the chart;
- WAL is on a second volume by default. The `pg_upgrade` and rollback pods
  now mount every claim, and the new cluster's WAL stays on the WAL volume;
  measured in a container, not yet on the chart;
- Patroni's labels are `app`, `cluster-name` and `release`; the names are
  discovered from Patroni's settings (above), checked with the chart's labels
  in unit tests only;
- after any `apply`, the chart's values still name the old image, and the next
  `helm upgrade` would put it back. Preflight says when Helm manages the
  StatefulSet, and the runbook, the script and `apply`'s report end with the
  image repository and tag to put in your values first.

Until that is done, chart users get the most from `preflight`, `plan` and
`plan --format script`.

---

## Requirements

- Python 3.8 or later, standard library only — nothing to install.
  `scripts/tsupgrade.sh` finds an interpreter and runs `python -m tsupgrade`.
- `kubectl`, pointed at the cluster. Preflight needs to read pods, exec into
  them, and read the StatefulSet, PVCs and the namespace, plus the
  cluster-scoped nodes and storage classes. `apply` and `rollback` also change
  the StatefulSet, pods, PVCs, endpoints and configmaps in the namespace, and
  start a pod there.
- `bash`. On Windows, Git Bash or WSL.

Every command below is `bash scripts/tsupgrade.sh <command>`; `--help` on any
of them lists its options.

---

## 1. Preflight

One command, run before you touch anything:

```bash
make preflight TARGET_IMAGE=timescale/timescaledb-ha:pg17.11-ts2.30.1
```

It answers, for the cluster you are actually pointed at:

| Check | What it establishes |
|---|---|
| Cluster and namespace | Echoes back the context, API server, Kubernetes version and namespace, so you can confirm you are where you think you are, and any selector, StatefulSet and container discovered from Patroni's settings. |
| Helm ownership | Whether Helm manages the StatefulSet (`app.kubernetes.io/managed-by: Helm`, or a `meta.helm.sh/release-name` annotation); if so, the image to put in the chart's values. Never a failure. |
| Target image exists | Queries the target's registry for the manifest. Under `apply`, every image the plan still has to run, by digest. |
| Target image on nodes | Whether the image is already cached on each node. |
| Running versions, per instance | PostgreSQL and TimescaleDB versions **queried from each database**, per member, and per database. |
| PostgreSQL patch level | Refuses the six patch releases that cannot load TimescaleDB (17.1, 16.5, 15.9, 14.14, 13.17, 12.21), and a target TimescaleDB that will not load on the server's minor. |
| Disk headroom | Free space where a dump would land, against the live database size. Before a `pg_upgrade`, free space on ordinal 0 for the copy or link. |
| Other extensions | Every other extension installed, and the libraries its C functions use, against what the target image ships. |
| Collation | Warns when a hop changes glibc under a database whose text is sorted by a libc collation. |
| WAL archiving | Before a `pg_upgrade`: flags `archive_mode = on`, since backups will not match the upgraded cluster. |
| Permissions | Ownership, mode and writability of the data directory and dump path, plus PVC capacity, access modes and whether the storage class can expand. |
| Drift | Each database against what its own image can install: `stranded` (the image cannot load the recorded version) and `ahead` (the image was rolled back under it) fail; `lagging` passes with an advisory. |
| Update-script gates | Objects the update scripts would refuse: old continuous aggregates, aggregates on `time_bucket_ng`, user views on `timescaledb_information`. |
| Matrix verdict | Looks the discovered versions up against the requested target. |

Versions come from `current_setting('server_version')` and `pg_extension` — never
from the image tag, and never from chart values. A tag records what someone
*intended* to deploy; only the database knows what is running. The extension
version is read **per database**, because they can legitimately differ, and a
disagreement is reported rather than flattened into one confident answer.

### It is read-only, and that is enforced

- Only `kubectl get` and `exec` touch the cluster (with the client-side
  `kubectl config` and `kubectl version`). Never `apply`, `patch`, `delete` or
  `scale`.
- Every connection opens with `PGOPTIONS=-c default_transaction_read_only=on`,
  so **PostgreSQL itself** rejects any statement that would write, whatever the
  SQL says.
- Commands run inside pods are `psql` (through `env`, to set that option) and
  `df`, `du`, `stat`, `id`, `test` and `ls`, an `ls` with a wildcard going
  through `sh -c`.

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
image. Under `apply` the one way past it is the operator's choice (see
[Where images come from](#where-images-come-from)).

---

## 2. Plan

`plan` decides and writes nothing; `apply` executes a plan and is the only
command that writes. They are separate so a plan can be read, diffed and
approved before anything touches a cluster.

```bash
bash scripts/tsupgrade.sh plan --from-pg 15.13 --from-ts 2.12.2 \
  --target-image timescale/timescaledb-ha:pg15.18-ts2.28.3 --output plan.json
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

**A source between rows takes two plans.** Rows start at a series the
harness verified from, so a cluster on an older series than any row (pg14
2.13 towards pg16, say) gets no plan of its own. When two verified rows
cover the way, the refusal names both: first the same-major update to the
series a row starts from, then the major change from there. Each plan is the
route its own records ran; they are not joined into one, because no record
ran the whole.

A plan is a list of hops: one image the cluster must run, then one `ALTER
EXTENSION timescaledb UPDATE TO '<v>'`. Known TimescaleDB bugs add stops: a
database updated from below 2.15 stops at 2.27.2 on the way to 2.28.2/2.28.3
([#10272](https://github.com/timescale/timescaledb/issues/10272)), and one on
1.x stops at 2.1.1 on the way to 2.10.2 or later
([#5789](https://github.com/timescale/timescaledb/issues/5789)).

---

## 3. Apply

```bash
bash scripts/tsupgrade.sh apply plan.json --report report.json
```

Each hop runs preflight against its image (any FAIL stops), rolls the
StatefulSet onto it if needed (replicas first, primary last), updates every
database in a fresh `psql -X` session, cycles client sessions so none keeps the
old library loaded, and confirms every database on every instance reports the
new version.

`apply` refuses, having written nothing, when the cluster is not in the state
the plan was made from, when an update step carries SQL other than the one
statement its version implies, when the StatefulSet does not update
`OnDelete` (with `RollingUpdate` the controller would replace pods itself,
the primary included, while `apply` deletes them in its own order), and when
the plan was built with `--verification-run` unless
`--acknowledge-verification-run` is also given.
It stops at the first failing step, keeps the error verbatim, and reports what
it had already done. It never retries or rolls back on its own.

`--database` scopes a run to named databases; without it every database with
TimescaleDB must be at the source version, because a real upgrade has to
cover them all.

**Helm.** `apply` changes the StatefulSet with `kubectl set image`, and the
chart's values still name the old image, so the next `helm upgrade` would
put it back. After a major upgrade, each pod restarted on the old image does
not start: PostgreSQL refuses the upgraded data directory ("database files are
incompatible with server"). Measured on kind under Patroni: the data is not
touched, and setting the new image back starts the pods again. `apply` does
not refuse. When Helm manages the StatefulSet and the run
changed its image, the report ends with the image repository and tag to put
in the values. The runbook and `plan --format script` always end with the same
note, worded "if a Helm chart manages this StatefulSet", since `plan` does
not look at the cluster.

---

## Changing the PostgreSQL major

A target on a newer major is planned as a route of `pg_upgrade` hops between
same-major segments, from the measured contents of the route's images (an
`-all` tag carries several majors' binaries, but not every major: none pairs
pg12 with anything past pg13). Each `pg_upgrade` runs inside an image
carrying both majors, at a TimescaleDB release both sides ship, so the old
major is updated to that release first. The route takes the fewest
`pg_upgrade` runs, and the same-major segments keep their stops: pg12 2.11 to
pg16 runs 12 → 13, 13 → 15 and 15 → 16, and stops at 2.27.2 on pg15 on the
way.

```bash
bash scripts/tsupgrade.sh plan --from-pg 12.22 --from-ts 2.11.2 \
  --target-image timescale/timescaledb-ha:pg16.15-ts2.30.1 --output plan.json
```

**How `apply` runs one.** Before anything stops, it reads the data directory
from the servers (`data_directory`, ordinal 0's first). It refuses if they
disagree, or if the path is not on a volume claim: the deepest mount that
covers it has to be one of the StatefulSet's volume claim templates, because
the upgrade pod mounts only those, and `pg_upgrade` writes beside the
directory. `--data-dir` overrides it. Only when no server answers does it fall
back to the images' `/home/postgres/pgdata/data`, and the report says so. A
major upgrade with tablespaces is refused for now (see
[Known limits](#known-limits)). Under Patroni
it then moves the leader to ordinal 0, scales the StatefulSet to 0, and runs
`pg_upgrade` in a pod on ordinal 0's disks (every volume claim template,
where the database container mounts it), scheduled like the database pods. That pod's `initdb` takes the old
cluster's data checksums, encoding, locale and locale provider (from its
`template0`), or `pg_upgrade --check` would refuse it mid-outage. When
`pg_wal` is a symlink to a volume of its own, the new cluster's WAL goes
there too (`initdb --waldir`, beside the old WAL) and is swapped like the data
directory: the old WAL is kept as `<wal>.pg<old major>`. Then `apply` deletes
the replicas' disks, every claim's, and Patroni's state in the DCS, since
`pg_upgrade` gives the cluster a new system identifier. Patroni's objects are
deleted by name, for the scope Patroni reports, and `apply` refuses up front
if the pod selector also matches endpoints or configmaps that are not
Patroni's. Last, it restarts everything on the new major's image; Patroni
re-clones the replicas. The plan ends with `vacuumdb --all
--analyze-in-stages`.

By default `pg_upgrade` copies, and the old data directory stays beside the
new one as `<data>.pg<old major>`, to go back to. `plan --link` hard-links
instead, which is fast and needs little space but leaves the old directory
unusable once the new cluster has started. Both modes are verified on kind
for every verified cross-PG row, and the records show what `pg_upgrade` did:
copied files, or hard links into the old directory.

`pg_upgrade` may write an `update_extensions.sql` holding a bare
`ALTER EXTENSION timescaledb UPDATE`. `apply` never runs it, and removes it:
run on the new cluster, it goes straight to that image's default release,
past any stop the plan makes (after 13 → 15, from 2.15.3 to 2.28.3, past the
2.27.2 stop).

**Waiting, and when something fails.** `apply` waits for `pg_upgrade` as long
as it runs (`--pg-upgrade-timeout` sets a limit; a pod that never starts
fails after `--rollout-timeout`). When it stops waiting, the pod keeps
running, and the report says how to watch it and lists the commands that
finish the upgrade. An upgrade pod already there is refused, never deleted.
Every failure after the cluster is stopped says what state it left and how to
go on. A failure before `pg_upgrade` has succeeded leaves the cluster stopped
with its data intact. After a failed `--link`, the report also says whether
linking had begun: if it had, `pg_upgrade` renamed the old cluster's
`global/pg_control` to `pg_control.old`, and the old major starts again
once it is renamed back (the documented recovery; the new cluster is never
started by then).

**Checked before anything stops.** Every `pg_upgrade` is preceded by a
preflight against the image the cluster restarts on. It adds three checks:

- *Disk on ordinal 0.* A copy needs the data directory's size free again;
  the check counts the whole directory, WAL included, so it errs high, since
  `pg_upgrade` copies the relation files and the new cluster starts its own
  WAL. `--link` needs only a margin. A WAL volume of its own needs room for the
  new cluster's WAL: the check asks for 256 MiB, a margin, not a measured
  figure. Each tablespace's volume is checked the same way as the data
  directory's. Each hop keeps its old directory, so
  a chain such as 12 → 13 → 15 → 16 by copying ends with three of them;
  preflight and `apply` list them, to remove once the result is accepted.
- *Other extensions.* The matrix verdicts cover TimescaleDB only. Preflight
  lists the other extensions installed in each database and the libraries
  their C functions use (as `pg_upgrade --check` selects them), and checks
  them against what the target image and the `pg_upgrade` image ship for the
  new major, measured (`make image-contents`). A missing library fails. This
  check runs before every same-major hop too.
- *Backups.* `archive_mode = on` is flagged (below).

Separately, the images do not share one glibc: the Debian 10 tags
(`pg11.11-ts2.1.1-p0`, `pg12.6-*`) carry 2.28, the Ubuntu 22.04 ones 2.35. A
database with a libc collation other than C/POSIX sorts its text indexes by
glibc's rules, so preflight warns when any hop changes glibc under one, and
names the check to run afterwards (`amcheck`'s `bt_index_check`, or
`REINDEX`).

**Backups break at a major upgrade.** `pg_upgrade` gives the cluster a new
system identifier, so backups and the WAL archive made before no longer
match it. Take a full backup before. Afterwards, with pgBackRest, run
`stanza-upgrade` (`--no-online` while the database is down) and take a new
full backup (pgBackRest's "Upgrading PostgreSQL", for the 2.54–2.59 the
images ship). The data directory keeps its path, so `pg1-path` does not
change. The runbook and `apply`'s report say the same.

---

## Going back from a copy

`tsupgrade rollback --report report.json --discard-writes-since-upgrade`
undoes the last major upgrade in an `apply` report. It stops the cluster,
puts `<data>.pg<old>` back in place (the new directory goes to
`<data>.rolled-back-pg<new>-<its system identifier>`, so a later upgrade and
rollback on the same disk does not collide with it), deletes the replicas'
disks and Patroni's state, and starts the old image again (with WAL on its
own volume, the old WAL comes back with the old directory), checking that the
old system identifier is back. Every write since the upgrade is lost,
extension updates included, which is why the flag is required. It refuses
after `--link`, and after an upgrade that did not finish (whose report says
how to start the old cluster instead). The data directory is the one the
report names; a report without one is refused unless `--data-dir` gives it.

---

## Without `apply`: a script for any deployment

`apply` drives a Patroni StatefulSet itself, so it cannot run under an
operator, which would fight it for the pods. The version stepping does not
depend on Patroni, and it is the part operators leave to you: the stops for
known TimescaleDB bugs, the images that ship each release, the release
`pg_upgrade` needs on both majors, never running `update_extensions.sql`.
`plan --format script` writes the plan as a commented shell script. It runs
the `ALTER EXTENSION`s and the version checks with `psql -X` against the
primary (`PG*` variables, `DATABASES`), and stops at each step only your
deployment can do (rolling out an image, the `pg_upgrade`), saying exactly
what that step must do.

```bash
bash scripts/tsupgrade.sh plan --from-pg 15.13 --from-ts 2.20.3 \
  --target-image timescale/timescaledb-ha:pg15.18-ts2.28.3 --format script > upgrade.sh
DATABASES="app metrics" PGHOST=... bash upgrade.sh
```

---

## Where images come from

Before its first hop, `apply` asks the registry for every image the plan has
to run, by digest, the `pg_upgrade` images included, so a missing one shows up
before anything stops. It asks once: a digest does not change, so an outage at
a later hop does not stop a run whose images were confirmed. If an image is
not there, `apply` stops. If the registry does not answer, `apply` also stops,
says that the image may well be fine, and lists what can be done:

- try again once the registry answers;
- `--image-registry HOST/PATH` (or `$TSUPGRADE_IMAGE_REGISTRY`) runs the images
  from `HOST/PATH/<image>:<tag>@<digest>`, any registry, no change to the plan.
  The digest stays, so the mirror must serve the very image that was verified.
  A copy of every platform keeps it (`skopeo copy --all`); a `docker pull` and
  `docker push` of one platform does not, and the check refuses it. It reaches
  the registry anonymously, so a private mirror does not answer it and needs
  the next option;
- `--use-cached-images` goes on without the registry, only if each unanswered
  image is on every node by digest and the StatefulSet's `imagePullPolicy` will
  not force a pull. `Always` does, even for a cached image (measured), and
  `apply` prints the `kubectl patch` that changes it rather than changing it.

The nodes' image lists can lag by a minute or so and show at most 50 images by
default, so a cached image can look absent, which stops the run, and a removed
one can look present until the list catches up. A pod that later moves to a node
without the image will not start until the registry answers. `plan
--image-registry` names the mirror in the runbook and the script; the plan file
keeps the verified route either way.

---

## Old continuous aggregates

An aggregate in the old format (created before 2.7, or up to 2.13 with
`finalized = false`) cannot cross the 2.25 update, which also removes
`cagg_migrate`, TimescaleDB's procedure for converting it. If a plan reaches
2.25 with one,
`apply` stops before changing anything, says this came from a read-only check,
and shows each aggregate: its database and name, what it is built on, its size
and chunks, its jobs, the views on it and its grants. At a terminal it asks
y/n; `--migrate-caggs` is the same yes for scripts; without either it stops.
Agreeing does not run anything yet: an old database cannot migrate (2.1.1 has
no `cagg_migrate`), so `apply` does it at the first stop on the route from 2.14
to 2.24, with:

```sql
CALL cagg_migrate('<aggregate>', override => true, drop_old => true);
```

Without those two options the old aggregate stays and 2.25 still refuses, and a
second call fails with `plan already exists` (measured), so a plain `CALL
cagg_migrate('<aggregate>')`, which is what TimescaleDB's own hint says, is a trap.
The call drops the old aggregate for good, so have a backup. It also drops grants
without a word, so `apply` puts them back, with the owner, and runs the refresh the
call asks for. A refresh policy is kept, with its own offsets and schedule, under a
new job id. Compression on the aggregate and its compression policy are kept too,
but chunks that were compressed come back uncompressed until the policy runs, so
the aggregate takes its uncompressed size on disk meanwhile. Some cases stop even
with the flag: a view on the aggregate, and a route with no stop from 2.14 to
2.24. Aggregates on `time_bucket_ng` have their own section, below.

A view on the aggregate makes the call fail partway: it leaves `<name>_old` behind,
still blocking 2.25, and cannot be retried. So `apply` names each view and the
aggregate it is on, and prints the commands, for this cluster, to save it with
`pg_dump --schema-only -t`, drop it, and recreate it from that file once `apply` has
finished (and refresh it, if it is a materialized view). The file keeps the view's
definition, owner, grants and comment. That sequence is measured on 2.15.3, for a
view and a materialized view; loading the file on the new major relies on pg_dump
output loading into newer PostgreSQL, which its documentation states.

If the check cannot read a database, or finds no primary, `apply` stops at the
start too, says why with psql's own error, and that nothing has been changed:
finding out at the 2.25 hop would be after the pg_upgrades before it.

### Aggregates on `time_bucket_ng`

A database with a continuous aggregate built on `time_bucket_ng` cannot be
updated past 2.14. The 2.14 to 2.15 update tries to convert it and fails ("tried
calling catalog_get when extension isn't loaded"), and so does every update to
2.15.0-2.24.0; 2.25.0 and later fail in a catalog rewrite instead. Each one rolls
back, so nothing breaks, but nothing moves either. `cagg_migrate` refuses these
aggregates ("does not require any migration"), whatever the 2.25 update's hint
says, and upstream's fix (#7763) was closed unmerged. All of that was measured on
the images this tool uses, not taken from the documentation. Such aggregates can
be created from 2.4.0 to 2.14.2.

If a plan goes past 2.14 with one, `apply` stops before changing anything, the
same way, and shows each one with the `time_bucket` form it would be rebuilt on.
With y or `--migrate-caggs` it rebuilds each, at the source or at the first stop
on the route from 2.8 to 2.14, before any update past 2.14:

1. it builds the same aggregate on `time_bucket` beside the old one;
2. it keeps it only if `time_bucket_ng` and `time_bucket` put every point of a
   dense series from 2001 to 2041 in the same bucket, and the two aggregates'
   queries return the same rows on your live data (averages within 1e-9);
3. it materializes it over the range the old one had materialized;
4. in one transaction it drops the old one, gives the new one its name, and puts
   back the refresh policy, owner, grants, comment and real-time setting.

Translating the definition is not enough. `time_bucket_ng` ignores some origins
that `time_bucket` honours, and the two start weeks on different days, so `apply`
tries the literal form, then without the origin, then with `time_bucket_ng`'s own
origin, and keeps the first one the comparison proves. The rebuild drops the old
aggregate for good, so have a backup. It needs about the aggregate's size free
again and takes as long as rebuilding that data.

It stops, leaving the aggregate as it was, when:

- no `time_bucket` form buckets the same way. At 2.14 and below an aggregate
  takes an origin only with a timezone, so a weekly bucket on `timestamp` or
  `date` has no equivalent. Recreate it yourself, knowing its weeks will start
  on another day, or drop it;
- the aggregate holds rows the raw data no longer has, because a retention policy
  dropped them. A rebuild would lose them, so copy them to a table first;
- it has an index of its own, a job other than its refresh policy, or
  compression: drop them, run `apply` again, and recreate them afterwards;
- a view depends on it (the same commands as above), or the route never stops
  from 2.8 to 2.14.

---

## The version matrix

Which upgrades are established lives in
[`data/upgrade-matrix.json`](data/upgrade-matrix.json), published as
[`docs/matrix.md`](docs/matrix.md). The format, the verification rule, the
image catalog and `make lookup` are in
[`docs/matrix-method.md`](docs/matrix-method.md).

**What verified means.** A row is verified only by running that migration with
this tool on a disposable kind cluster (`verify/harness.py`): once on an empty
database and once on a seeded one, and for a PostgreSQL major change both again
with `pg_upgrade --link`. Documentation and changelogs are cited, never taken as
proof. The one exception is `no_image`, which the container registry answers.

**Tests, verification runs and the matrix are three different things.**

- The **test suite** (`python -m pytest`) checks the code and the shipped data.
  It needs no cluster, and it is green or the change does not go in.
- A **verification run** (`verify/harness.py`) runs one matrix row end to end
  with `plan` and `apply` on a kind cluster, then checks the data. It is the
  end-to-end test of the tool, and it leaves a **record** in
  [`verify/evidence/`](verify/evidence/): what happened, and whether the
  checks held. A record is an observation, not a test result: a control's
  record shows an update breaking, because that is what it is for.
- The **matrix** holds one verdict per row, written from that row's records.
  The test suite fails when a verdict and its records disagree.

The matrix is established per release. As of `v0.2.1`, all 131 rows are verified: 126 by running them (98 same-PG, 28 that change
major) and 5 `no_image` rows by the registry. Every record names the commit it
ran from, on a clean tree, and each row's route is pinned by digest. Each row is proven at
the PostgreSQL minors and image digests its records name, on a fixture of about
50,000 rows: it shows that the route works, not how long it takes on your data.

A record made before a later change to the code that decides a run is stale.
On a development branch the tests only warn about stale records; `make
release-check`, and the tests on a tagged release, fail on them. A release
re-runs what is stale first ([details](docs/matrix-method.md#what-makes-a-row-verified)).

Every run a verdict cites passed; the test suite fails otherwise. A pass can
still carry a caveat, written in the row's reason. On ten rows (pg13 2.1-2.6 and
pg14 2.5-2.6 same-PG, pg11 2.1 to pg13 and pg14) a continuous aggregate created
before 2.7 comes through intact but can no longer be altered from 2.14.0, so
the seeded checks read it below its watermark; the row says so and gives the
step. pg11 2.1 to pg15-pg18 migrate the aggregate on the way (see
[Old continuous aggregates](#old-continuous-aggregates)).

---

## Known limits

- The archived Helm chart's layout is not supported by `apply` and `rollback`
  yet (see [Who can use it today](#who-can-use-it-today)).
- `--image-registry` reaches the registry anonymously, so a private mirror
  needs `--use-cached-images`. No run went through a real mirror: the harness
  uses a host that cannot resolve, and a registry that answers is stubbed in
  the tests.
- A registry that does not answer stops `apply` unless `--use-cached-images` is
  given.
- The reset of Patroni's state deletes `<scope>-sync` if present and labelled,
  but synchronous mode was not measured.
- That WAL archiving fails until `stanza-upgrade` has run is inferred from
  pgBackRest checking the system identifier, not observed. The rest of the
  backup advice is from pgBackRest's user guide.
- The old-aggregate migration was run through 2.15.3 on PostgreSQL 13, on one
  aggregate per database on kind, and in a container also with two aggregates
  in one database, a grant to restore, compression with its policy, and a
  refresh policy with its own settings. An aggregate on an old-format
  aggregate cannot exist (both 2.1.1 and 2.15.3 refuse to create one). A
  dependent view stops the run instead.
- The `time_bucket_ng` rebuild was run in a container at 2.8.0, 2.12.2 and 2.14.2
  on PostgreSQL 13, on the shapes `time_bucket_ng` takes in an aggregate (day,
  week and month; with and without a timezone and an origin; on `timestamptz`,
  `timestamp` and `date`), and through the harness on PostgreSQL 15 from 2.12.2
  and 2.14.2 to 2.28.3. It compares the live data in full, which on a large
  aggregate is as slow as the rebuild itself.
- A major upgrade with tablespaces is refused before anything stops. Support
  is planned but not there: it waits on a run through `apply` on a cluster with
  a tablespace volume. What was measured so far, in containers: `pg_upgrade`
  12 → 13 kept a hypertable's chunks, compressed ones included, in a tablespace
  on a mount of its own, copying and with `--link`, and the rollback brought
  them back; and on kind, Patroni re-cloned replicas onto empty tablespace
  volumes.
- WAL on its own volume, and the data directory at the chart's path, were run
  through the real `pg_upgrade` and rollback scripts in a container (copy and
  `--link`), not yet through `apply` on a cluster with that layout.
- The disk check before a `pg_upgrade` measures the data directory with `du`
  under preflight's exec timeout; on a very large database it reports
  `unknown`, not a failure.

---

## Try it locally

The test suite needs no cluster, no Docker and no network:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests that run a real `timescaledb-ha` container skip themselves when Docker or
the image is missing. Establishing matrix rows is a separate, much longer run
on kind (below).

```bash
make up      # a kind cluster with TimescaleDB under Patroni
make smoke   # connect, create a hypertable, insert, query
make down    # destroy everything
```

Requirements, seeding, knobs and how the cluster is built:
[`docs/local-cluster.md`](docs/local-cluster.md). The verification harness that
establishes matrix rows: [`verify/README.md`](verify/README.md).

---

## Layout

```
cluster/           kind cluster definition (node image pinned by digest)
deploy/            Kubernetes manifests with __PLACEHOLDER__ tokens
scripts/           shell entry points; the Makefile is just a table of contents
  lib.sh           shared config, rendering, psql/kubectl helpers
  tsupgrade.sh     runs the CLI with a Python 3.8+ it finds
data/
  images.json           generated: which image tags the registry actually serves
  image-contents.json   generated: what specific images can really install
  upgrade-matrix.json   the compatibility matrix
docs/
  matrix.md             generated: the published version table
  matrix-method.md      the matrix's format and the verification rule
  local-cluster.md      the local test cluster
tools/tsupgrade/   the CLI (`python -m tsupgrade`), standard library only
  preflight.py     the read-only pre-upgrade checks
  plan.py          builds the runbook; writes nothing
  majors.py        routes across PostgreSQL majors (pg_upgrade hops)
  steps.py         the plan's step types, read from JSON in one place
  apply.py         executes a plan
  rollback.py      undoes the last copy-mode major upgrade
  scripts/         the shell scripts the pg_upgrade and rollback pods run
verify/            the verification harness, its fixture and the run evidence
tests/             unit tests, plus container tests on real pg_upgrade binaries
```

---

## Roadmap

- [x] Local test cluster: `make up`, `make down`, seeding, smoke test
- [x] Version matrix as data, with a loader, lookup and published table
- [x] Read-only preflight checks against a live instance
- [x] Drift classifier, update-script gates, measured image contents
- [x] `plan` and `apply` (extension updates and image rollouts)
- [x] Verification harness; every row established by running it with the
      upgrader, or by the registry where no image exists
- [x] PostgreSQL major upgrades (`pg_upgrade` under Patroni, copy or `--link`)
- [x] Rollback of a copy-mode major upgrade (`tsupgrade rollback`)
- [x] `plan --format script`, for deployments `apply` cannot drive
- [ ] The archived Helm chart's layout, verified as a second harness topology
- [ ] Rehearsal on a copy of your own data
- [ ] Maintenance-window estimation

---

## License

[MIT](LICENSE)
