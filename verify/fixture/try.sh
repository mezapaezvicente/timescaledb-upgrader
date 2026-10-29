#!/usr/bin/env bash
# Prove the fixture is creatable at given source versions on one image.
#
#   bash verify/fixture/try.sh <image-tag> <series>...
#   bash verify/fixture/try.sh pg13.22-ts2.15.3 2.1 2.7 2.14
#
# One plain container per image (no kind), one database per version. For each
# series, the newest patch the image really ships (data/image-contents.json)
# is installed with CREATE EXTENSION ... VERSION and seeded. For 2.x the
# checks then run at that same version, which tests check.sql itself; 1.x
# cannot run them (check.sql is 2.x syntax), so it is seeded only.
#
# This is not an upgrade run and nothing it prints is a matrix result.
# Logs go to verify/out/fixture/<tag>/.
set -uo pipefail
export MSYS_NO_PATHCONV=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="${REPO_ROOT}/verify/fixture"
TAG="$1"; shift
IMAGE="timescale/timescaledb-ha:${TAG}"
NAME="fx-${TAG//./-}"
OUT="${REPO_ROOT}/verify/out/fixture/${TAG}"
mkdir -p "$OUT"

PY="${PYTHON:-python}"
TOOLS="${REPO_ROOT}/tools"
# MSYS_NO_PATHCONV also stops Git Bash translating env vars for Windows Python.
command -v cygpath >/dev/null 2>&1 && TOOLS="$(cygpath -w "$TOOLS")"
resolve() {  # newest installable patch of a series on this image's own major
  PYTHONPATH="$TOOLS" "$PY" -c '
import sys
from tsupgrade.contents import load_contents
from tsupgrade.versions import parse_version
tag, series = sys.argv[1], parse_version(sys.argv[2])
major = int(tag[2:].split(".")[0])
p = load_contents().profile("timescale/timescaledb-ha:" + tag, major)
hits = [v for v in p.installable if parse_version(v)[:2] == series[:2]]
print(hits[-1] if hits else "")' "$TAG" "$1"
}

psql_in() { docker exec -i "$NAME" psql -U postgres -X -v ON_ERROR_STOP=1 "$@"; }

docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=postgres "$IMAGE" >/dev/null || exit 1
trap 'docker rm -f "$NAME" >/dev/null 2>&1' EXIT
# The entrypoint runs initdb behind a temporary server and then restarts, so
# pg_isready alone can answer for the server that is about to go away.
for _ in $(seq 1 120); do
  docker logs "$NAME" 2>&1 | grep -q "init process complete" &&
    docker exec "$NAME" pg_isready -U postgres -q 2>/dev/null && break
  sleep 1
done
docker exec "$NAME" pg_isready -U postgres -q || { echo "$TAG: server never became ready"; exit 1; }

failed=0
for series in "$@"; do
  version="$(resolve "$series")"
  if [ -z "$version" ]; then
    echo "$TAG $series  SKIP  image ships no ${series}.x"
    failed=1; continue
  fi
  db="fx_${version//./_}"
  log="${OUT}/${version}.log"
  {
    psql_in -d postgres -c "DROP DATABASE IF EXISTS ${db}" -c "CREATE DATABASE ${db}" &&
    psql_in -d "$db" -c "DROP EXTENSION IF EXISTS timescaledb CASCADE" &&
    # A fresh session: up to at least 2.1, the library DROP loaded stays
    # loaded, and CREATE of another version in that session is refused.
    psql_in -d "$db" -c "CREATE EXTENSION timescaledb VERSION '${version}'" &&
    psql_in -d "$db" -qtA -f - < "${HERE}/seed.sql"
  } > "$log" 2>&1
  if [ $? -ne 0 ]; then
    echo "$TAG $version  SEED-FAIL  $(grep -m1 -E 'ERROR|FATAL' "$log")"
    failed=1; continue
  fi
  case "$version" in
    1.*) echo "$TAG $version  seeded  (1.x: checks need a 2.x target)"; continue ;;
  esac
  checks="$(docker exec -i "$NAME" psql -U postgres -X -qtA -F'|' -d "$db" \
              -v expect="$version" -f - < "${HERE}/check.sql" 2>>"$log")"
  echo "$checks" >> "$log"
  want="version rows still_compressed cagg_survived cagg_matches write_and_refresh decompress"
  bad=""
  for c in $want; do
    echo "$checks" | grep -q "^check|${c}|pass|" || bad="${bad} ${c}"
  done
  if [ -n "$bad" ]; then
    echo "$TAG $version  CHECK-FAIL:${bad}  $(grep -m1 -E 'ERROR' "$log")"
    failed=1
  else
    echo "$TAG $version  ok  (7/7 checks)"
  fi
done
exit $failed
