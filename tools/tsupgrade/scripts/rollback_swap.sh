#!/usr/bin/env bash
# rollback: put the directory pg_upgrade kept back in place, on ordinal 0's
# disk, in a one-off pod on the old image. Environment (set by rollback.py):
# FROM, TO (majors: back to FROM), DATA, KEPT (<data>.pg<FROM>), SYSID (the
# system identifier of TO's cluster, so a second upgrade and rollback on the
# same disk finds its own name free).
#
# WAL on its own volume: pg_upgrade.sh pointed the kept directory's pg_wal at
# <wal>.pg<FROM>, and the new one's at <wal>. Those symlinks say what to swap
# back; the new cluster's WAL goes aside under the same name as its data.
set -euo pipefail
: "${FROM:?}" "${TO:?}" "${DATA:?}" "${KEPT:?}" "${SYSID:?}"
data="$DATA"; keep="$KEPT"; aside="$data.rolled-back-pg$TO-$SYSID"
[ -d "$keep" ] || { echo "$keep is not there; nothing to go back to"; exit 2; }
[ -e "$keep/global/pg_control.old" ] && { echo "$keep was upgraded with --link; it is not usable"; exit 2; }
[ -e "$aside" ] && { echo "$aside already exists; refusing to overwrite it"; exit 2; }
wal=""; wal_keep=""; wal_aside=""
if [ -L "$keep/pg_wal" ]; then
  wal_keep=$(readlink "$keep/pg_wal"); wal="${wal_keep%.pg"$FROM"}"
  [ "$wal" != "$wal_keep" ] || { echo "$keep/pg_wal points to $wal_keep, not to a kept <wal>.pg$FROM; not touching it"; exit 2; }
  [ -d "$wal_keep" ] || { echo "$wal_keep is not there; nothing to go back to"; exit 2; }
  [ "$(readlink "$data/pg_wal")" = "$wal" ] || { echo "$data/pg_wal does not point to $wal; not touching it"; exit 2; }
  wal_aside="$wal.rolled-back-pg$TO-$SYSID"
  [ -e "$wal_aside" ] && { echo "$wal_aside already exists; refusing to overwrite it"; exit 2; }
fi
mv "$data" "$aside"
mv "$keep" "$data"
if [ -n "$wal" ]; then
  mv "$wal" "$wal_aside"
  mv "$wal_keep" "$wal"
  ln -sfn "$wal_aside" "$aside/pg_wal"
  ln -sfn "$wal" "$data/pg_wal"
  echo "PostgreSQL $TO's WAL is now $wal_aside; PostgreSQL $FROM's is back at $wal"
fi
echo "PostgreSQL $TO's data directory is now $aside; PostgreSQL $FROM's is back at $data"
