# Drift classifier and update-script gates — what was executed

Companion to the unit tests, which cover the decisions without a cluster. This
records the parts that were run against real infrastructure, so that anyone
picking it up can re-run them rather than trust them.

Executed 2026-09-20 against `timescale/timescaledb-ha:pg16.15-ts2.30.1`, as a
standalone container and as a single-node kind cluster under Patroni.

## What an image can install

`pg_available_extension_versions` is not the answer, and the way it is wrong is
expensive:

| Source | Says | Floor |
|---|---|---|
| `pg_available_extension_versions` | 56 versions | 2.12.2 |
| Image label `…timescaledb.available_versions` | 56 versions | 2.12.2 |
| Install scripts ∩ shared objects | **55 versions** | **2.13.0** |

2.12.2 has neither `timescaledb--2.12.2.sql` nor `timescaledb-2.12.2.so`.
PostgreSQL lists it because *update scripts referencing it* exist, and the label
is the union across a multi-PostgreSQL build in which 2.12 only ever served
pg15. Requesting it does not fail cleanly — the backend closes the connection —
so anything trusting either source and then issuing version-specific DDL looks
like a crash rather than a validation error.

The directories come from the `pg_config` **view**, which is ordinary read-only
SQL. That matters: it means the classifier adds `ls` to the in-pod command set
and nothing else.

```sh
docker exec tsdrift psql -U postgres -X -qtA \
  -c "SELECT setting FROM pg_config WHERE name IN ('SHAREDIR','PKGLIBDIR')"
docker exec tsdrift ls /usr/share/postgresql/16/extension/
docker exec tsdrift ls /usr/lib/postgresql/16/lib/
```

### Measured once per image, then shipped

Running that per image by hand does not scale, and the labels are not a
substitute, so `tsupgrade contents measure` does it for every image the
repository uses and writes `data/image-contents.json`. Measured 2026-10-29:

| Image | Installable (own PG major) | Labels said |
|---|---|---|
| `pg11.11-ts2.1.1-p0` | 1.6.0 .. 2.1.1 | no version labels |
| `pg12.6-ts2.0.1-p0` | 1.7.0 .. 2.0.1 (pg11: 1.6.0 .. 2.0.1) | no TimescaleDB labels |
| `pg12.22-ts2.11.2` | **1.7.5, 2.1.0 .. 2.11.2** | 1.7.5 .. 2.11.2, no hole |
| `pg13.22-ts2.15.3` | 2.1.0 .. 2.15.3 | 2.1.0 |
| `pg14.19-ts2.19.3` | 2.5.0 .. 2.19.3 | 2.4.2 |
| `pg15.13-ts2.20.3` | 2.9.0 .. 2.20.3 | 2.8.1 |
| `pg15.18-ts2.28.3` | **2.13.0** .. 2.28.3 | **2.12.2** |
| `pg16.15-ts2.30.1` | 2.13.0 .. 2.30.1 | 2.12.2 |
| `pg17.11-ts2.30.1` | 2.17.0 .. 2.30.1 | 2.16.1 |
| `pg18.6-ts2.30.1` | 2.23.0 .. 2.30.1 | 2.22.1 |

Six of the eight labelled floors are one series low (pg12.22 and pg13 are
right). The registry clamp corrected the others, but not `pg15.18-ts2.28.3`, because pg15 images from 2.9 exist: `plan`
then sent a 2.12.2 database onto it in one hop. With the measurement it is
refused there and bridged through `pg15.13-ts2.20.3`.

The pg12 hole changes routing, not only acceptance. A pg12 database at 1.7
bridged through `pg12.6-ts2.0.1-p0` must stop at 1.7.5; landing on that image's
ceiling (2.0.1) leaves it on a version `pg12.22` cannot load. The bridge search
is breadth-first over every installable version for that reason. It also means
a pg12 database already **at 2.0** cannot reach 2.11 through these two images at
all; that needs a pg12 image shipping both 2.0.x and 2.1+.

```sh
make image-contents                       # docker run --rm, ls only, no server
bash scripts/tsupgrade.sh contents show
bash scripts/tsupgrade.sh plan --from-pg 15.18 --from-ts 2.12.2   --target-image timescale/timescaledb-ha:pg15.18-ts2.28.3   --via timescale/timescaledb-ha:pg15.13-ts2.20.3 --verification-run
```

## The four states, from real databases

Produced by pinning `CREATE EXTENSION timescaledb VERSION '<v>'` in separate
databases on one image, then reading `extversion` back:

| Database | `extversion` | State |
|---|---|---|
| `d_aligned` | 2.30.1 | `aligned` |
| `d_mid` | 2.20.3 | `lagging` |
| `d_lagging` | 2.13.0 | `lagging` |

`stranded` and `ahead` cannot be produced on a single image by construction —
they are statements *about* an image boundary — so they are verified offline
against real registry labels, which is also the only useful time to ask:

```
$ tsupgrade drift --image timescale/timescaledb-ha:pg15-ts2.28-all \
    --installed 2.9.2 --pg-major 15 --via timescale/timescaledb-ha:pg15-ts2.20
state   : STRANDED
bridge  : 2 image rollout(s)
  1. roll to …:pg15-ts2.20      ALTER EXTENSION  2.9.2  -> 2.20.3
  2. roll to …:pg15-ts2.28-all  ALTER EXTENSION  2.20.3 -> 2.28.3
```

Exit codes: `0` for `aligned`/`lagging`, `2` for `stranded`/`ahead`.

## One image tests every version it ships

The expensive assumption — that testing *N* source versions needs *N* images* —
is false, which is what makes verifying the same-PostgreSQL matrix rows
affordable. 2.13.0 reached 2.30.1 in a single statement, seventeen minor series
skipped:

```
CREATE EXTENSION timescaledb VERSION '2.13.0';   -- installs 2.13.0
ALTER EXTENSION timescaledb UPDATE;              -- lands on 2.30.1
```

Notes from doing it: the image installs the extension into new databases
automatically, so `DROP EXTENSION` first or the version pin is ignored; the
startup banner arrives as a `WARNING`, so check `pg_extension` for ground truth
rather than grepping output.

## The gates

Reproduced verbatim, including the rollback:

```
ERROR:  cannot drop view timescaledb_information.hypertables because other objects depend on it
DETAIL:  view public.my_ht depends on view timescaledb_information.hypertables
```

The extension stayed at 2.13.0 — the update is transactional, so this is a
blocked upgrade, not a broken database.

### Every gate query must probe for its own columns

The research identified one instance of this. There are at least three, because
the columns a gate inspects are exactly the ones the upgrade path adds or
removes:

| Column | Absent when | Consequence if unprobed |
|---|---|---|
| `continuous_agg.finalized` | 2.25 and later | `column "finalized" does not exist` |
| `hypertable.replication_factor` | **2.14 and later** | `column "replication_factor" does not exist` |
| `continuous_aggs_bucket_function.bucket_func` | **before ~2.14** | `column bf.bucket_func does not exist` |

Measured:

| Database | ts | `finalized` | `replication_factor` | `bucket_func` |
|---|---|---|---|---|
| `d_blocked` | 2.13.0 | present | present | **absent** (schema uses `name` + `experimental`) |
| `d_mid` | 2.20.3 | present | absent | present |
| `d_aligned` | 2.30.1 | **absent** | absent | present |

The third one bites hardest: without it the `time_bucket_ng` gate goes
indeterminate on exactly the old databases it exists to protect, since a 2.13
database upgrading to 2.30 passes straight through the 2.25 block. Both catalog
shapes are therefore supported.

A column the upgrade *removed* means the database is already past that cutoff,
so the gate is **vacuously satisfied**. A column that does not exist *yet* means
the same question has to be asked a different way. Neither is a failure.

### Second blocker at 2.25

`2.24.0--2.25.0.sql` carries two independent `RAISE EXCEPTION` blocks, not one:
non-finalized aggregates, **and** aggregates built on `time_bucket_ng`. Either
aborts the update alone, so both are checked. The `time_bucket_ng` detection
query is read from the shipped SQL rather than executed against a positive case
— creating such an aggregate is already refused at 2.13.0 (*"continuous
aggregate view must include a valid time bucket function"*), so no positive
fixture could be built on this image.

## Sources re-fetched, not recalled

| Claim | Verified by | Result |
|---|---|---|
| `views_detached.sql` guard keys off the **source** version | `sql/CMakeLists.txt` @ 2.30.1 | `if(START_VERSION VERSION_GREATER "2.24.0")` — strictly greater, so 2.24.0 itself is still exposed |
| The fix landed in 2.28.0 | HTTP status on raw URLs | 404 through 2.27.1, 200 from 2.28.0 |
| Multi-node has three triggers | `2.13.1--2.14.0.sql` | `replication_factor > 0`, `timescaledb_fdw` server, `dist_uuid` key |
| 2.25 drops `cagg_migrate` | `2.24.0--2.25.0.sql` | `DROP PROCEDURE IF EXISTS @extschema@.cagg_migrate` plus ~14 supporting routines |

## Registry floors: two sources that disagree

The published support matrix starts PostgreSQL 15 at TimescaleDB **2.10**. The
registry publishes **pg15/2.9** images, and `data/images.json` records them.

The clamp follows the registry. An image that exists is a stronger fact than a
table that omits it, and clamping to 2.10 would make the two-hop rescue above
unrepresentable — the very case the bridge search exists for. The disagreement
is reported in the command's output rather than resolved silently.

## The test suite

261 tests, no cluster and no network. Everything above that needed real
infrastructure is reproduced here as a scripted fixture, so the decisions stay
covered on a laptop with Docker switched off.

| File | Tests | Covers |
|---|---|---|
| `test_drift.py` | 49 | the four states, what an image can install, floor clamping, bridging |
| `test_gates.py` | 37 | gate applicability, catalog probes, clear vs. could-not-tell |
| `test_support.py` | 36 | the ABI table, the support matrix, registry/table disagreement |
| `test_images.py` | 32 | tag parsing, catalog queries, label parsing |
| `test_matrix.py` | 42 | matrix loader and lookup |
| `test_versions.py` | 30 | version parsing and match rules |
| `test_preflight.py` | 27 | status precedence, df parsing, matrix verdicts |
| `test_shipped_data.py` | 8 | the shipped JSON obeys its own rules |

### How the database is faked

`gates.py` takes its query runner as an argument — `(database, sql) -> (rc,
stdout, stderr)` — specifically so the gates can be driven without a cluster.
`tests/test_gates.py` supplies a `FakeDB` that answers column probes from a set
of `(table, column)` pairs and everything else from substring-keyed rows, with
`fail_on` to make a named query fail. That is what makes it possible to test
the cases that matter most and are hardest to stage for real: a catalog missing
the column a gate wants to read.

The same shape is why `preflight.check_gates` can hand in its existing
read-only psql path unchanged.

### What the tests are actually defending

Most of them are ordinary. These are the ones that encode something that was
got wrong at least once, or that would fail silently:

- **Numeric, not lexical, version comparison.** As text `2.9.2` sorts *after*
  `2.13.0`. A database at 2.9.2 on a 2.13-default image is lagging; compared as
  strings it looks `ahead`, which reports an outage and advises rolling the
  image *forward* — the opposite of the truth, in the one case that matters.
  Pinned in `test_versions_compare_numerically_not_lexically` and again for the
  offline path and the label floor/ceiling.
- **A missing column is not a tripped gate.** Three separate tests assert that
  an absent `replication_factor`, `finalized` or `bucket_func` yields `clear` or
  a different query — never `blocked`, never a bare failure.
- **A missing column must not blank its siblings.**
  `test_missing_column_does_not_blank_the_other_two_triggers` stages an absent
  `replication_factor` *and* a real foreign server, and requires the gate to
  still block. This is why the multi-node check runs three statements rather
  than one.
- **A failed query is never `clear`.** `fail_on` drives each gate into an error
  and asserts `indeterminate`.
- **Unreadable is never `stranded`.** A stranded database and an unreachable
  one look identical from outside; conflating them invents an outage.
- **`ahead` outranks `stranded`.** Both are outages, but the remedies point in
  opposite directions — forward vs. back — so the precedence is pinned.
- **The 2.24.0 boundary is exclusive.** The upstream guard is `VERSION_GREATER`,
  so a source at exactly 2.24.0 is still exposed, and a series-only `"2.24"`
  is treated as exposed too because it pins less.
- **Support-matrix gaps stay gaps.** `documented_pg_majors("2.11")` is `None`,
  not `()` and not `False`.
- **Bridging terminates.** A candidate that accepts a version without raising
  it, a target offered as its own bridge, and an `ahead` version that cannot be
  bridged upward all return a problem rather than looping or inventing a chain.

One test caught a bug in *itself* rather than the code: asserting that the
legacy path never mentions `bucket_func` failed, because the table is named
`continuous_aggs_bucket_function` and the substring matches. The assertion now
checks the column reference `bf.bucket_func`.

### Two claims the suite deliberately does not make

- **`time_bucket_ng` has no positive fixture.** Creating such an aggregate is
  already refused at 2.13.0 (*"continuous aggregate view must include a valid
  time bucket function"*), so no positive case could be built on this image.
  The detection query is read from the shipped SQL — graded [R], not [E] — and
  the tests cover the dual-schema *dispatch*, not a real blocked upgrade.
- **Nothing asserts an upgrade is safe.** Every gate answers what blocks an
  upgrade. A `clear` result means no known blocker was found, which is not the
  same claim, and the matrix loader's rule is untouched: runtime findings are
  check results and never get written back into `data/upgrade-matrix.json`.

## Re-running

Unit tests need nothing installed beyond Python:

```sh
bash scripts/test.sh                 # 261 tests, no cluster, no network
```

Offline drift needs only network access to the registry:

```sh
bash scripts/tsupgrade.sh drift \
  --image     timescale/timescaledb-ha:pg15-ts2.28-all \
  --installed 2.9.2 --pg-major 15 \
  --via       timescale/timescaledb-ha:pg15-ts2.20
```

The live checks need Docker, or the kind harness for the in-cluster paths:

```sh
docker run -d --name tsdrift -e POSTGRES_PASSWORD=x \
  timescale/timescaledb-ha:pg16.15-ts2.30.1
make up && make preflight TARGET_IMAGE=timescale/timescaledb-ha:pg16.15-ts2.30.1
```
