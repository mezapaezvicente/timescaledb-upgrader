#!/usr/bin/env bash
# rollback: put the directory pg_upgrade kept back in place, on ordinal 0's
# disk, in a one-off pod on the old image. Environment (set by rollback.py):
# FROM, TO (majors: back to FROM), DATA, KEPT (<data>.pg<FROM>).
set -euo pipefail
: "${FROM:?}" "${TO:?}" "${DATA:?}" "${KEPT:?}"
data="$DATA"; keep="$KEPT"; aside="$data.rolled-back-pg$TO"
[ -d "$keep" ] || { echo "$keep is not there; nothing to go back to"; exit 2; }
[ -e "$keep/global/pg_control.old" ] && { echo "$keep was upgraded with --link; it is not usable"; exit 2; }
[ -e "$aside" ] && { echo "$aside already exists; refusing to overwrite it"; exit 2; }
mv "$data" "$aside"
mv "$keep" "$data"
echo "PostgreSQL $TO's data directory is now $aside; PostgreSQL $FROM's is back at $data"
