#!/usr/bin/env bash
# pg_upgrade on ordinal 0's disk, in a one-off pod on the image carrying both
# majors, as the StatefulSet's user. apply.py starts it and reads its log,
# which is the major_upgrade step's output: one message per stage.
#
# Environment (all set by apply): FROM, TO (majors), PG_BIN_OLD, PG_BIN_NEW,
# DATA (the data directory), LINK ("--link" or empty), and the old cluster's
# template0: ENCODING, LC_COLLATE, LC_CTYPE, LOCALE_PROVIDER (libc, icu or
# builtin), PROVIDER_LOCALE.
#
# WAL on its own volume: when DATA/pg_wal is a symlink, the new cluster's WAL
# goes beside the old one's (<wal>.new, on the same volume) and is swapped like
# the data directory: the old WAL is kept as <wal>.pg<FROM>, and each data
# directory's pg_wal is pointed at its own. rollback_swap.sh reads the
# symlinks to swap them back.
#
# On failure the EXIT trap says what was changed ("old cluster disabled:",
# "old data directory moved:", "data directories swapped:", "old WAL directory
# moved:"), which apply turns into how to go back. It reports only once this
# run has started changing the disks: a refusal at the start says nothing, so
# what an earlier run left is never reported as this run's doing.
set -euo pipefail
: "${FROM:?}" "${TO:?}" "${PG_BIN_OLD:?}" "${PG_BIN_NEW:?}" "${DATA:?}" "${LINK?}"
: "${ENCODING:?}" "${LC_COLLATE:?}" "${LC_CTYPE:?}" "${LOCALE_PROVIDER:?}" "${PROVIDER_LOCALE?}"
old_bin="$PG_BIN_OLD"; new_bin="$PG_BIN_NEW"; data="$DATA"
new="$data.new"; keep="$data.pg$FROM"
wal=""; wal_new=""; wal_keep=""
if [ -L "$data/pg_wal" ]; then
  wal=$(readlink -f "$data/pg_wal")
  wal_new="$wal.new"; wal_keep="$wal.pg$FROM"
fi
cd "$(dirname "$data")"
changing=""

on_exit() {
  local rc=$? d
  if [ "$rc" -ne 0 ] && [ -n "$changing" ]; then
    for d in "$data" "$keep"; do
      if [ -e "$d/global/pg_control.old" ]; then echo "old cluster disabled: $d/global/pg_control.old"; fi
    done
    if [ -d "$keep" ] && [ ! -d "$data" ]; then echo "old data directory moved: $keep"; fi
    if [ -d "$keep" ] && [ -d "$data" ]; then echo "data directories swapped: $data"; fi
    if [ -n "$wal" ] && [ -d "$wal_keep" ]; then echo "old WAL directory moved: $wal_keep"; fi
  fi
  exit "$rc"
}
trap on_exit EXIT

for b in "$old_bin" "$new_bin"; do
  [ -x "$b/pg_upgrade" ] || [ -x "$b/postgres" ] || { echo "this image has no $b"; exit 2; }
done
[ -e "$keep" ] && { echo "$keep already exists; refusing to overwrite it"; exit 2; }
[ -e "$data/global/pg_control.old" ] && {
  echo "$data/global/pg_control.old exists: an earlier pg_upgrade --link disabled this cluster;" \
       "rename it back to $data/global/pg_control before upgrading again"; exit 2; }
if [ -n "$wal" ]; then
  [ -d "$wal" ] || { echo "$data/pg_wal points to $wal, which is not there"; exit 2; }
  [ -e "$wal_keep" ] && { echo "$wal_keep already exists; refusing to overwrite it"; exit 2; }
  rm -rf "$wal_new"
fi
rm -rf "$new"

# Data checksums must match, and PostgreSQL 18's initdb turns them on by default.
sums=$("$old_bin/pg_controldata" "$data" | sed -n 's/^Data page checksum version: *//p')
args=(-U postgres)
if [ "$sums" != 0 ]; then
  args+=(--data-checksums)
elif "$new_bin/initdb" --help | grep -q -- --no-data-checksums; then
  args+=(--no-data-checksums)
fi
# The old cluster's encoding, locale and provider (from its template0), or
# pg_upgrade --check refuses the new cluster (13-15) during the outage.
args+=(-E "$ENCODING" --lc-collate="$LC_COLLATE" --lc-ctype="$LC_CTYPE")
case "$LOCALE_PROVIDER" in
  icu) args+=(--locale-provider=icu --icu-locale="$PROVIDER_LOCALE") ;;
  builtin) args+=(--locale-provider=builtin --builtin-locale="$PROVIDER_LOCALE") ;;
esac
if [ -n "$wal" ]; then args+=(--waldir="$wal_new"); fi
echo "old cluster: PostgreSQL $FROM, data checksums ${sums:-unknown}; initdb ${args[*]}"
"$new_bin/initdb" "${args[@]}" -D "$new" >/dev/null
echo "shared_preload_libraries = 'timescaledb'" >> "$new/postgresql.conf"

report() {
  echo "pg_upgrade $1 failed:"
  tail -n 40 "$new"/pg_upgrade_output.d/*/*.txt "$new"/pg_upgrade_output.d/*/log/* 2>/dev/null || true
}
upgrade=("$new_bin/pg_upgrade" -b "$old_bin" -B "$new_bin" -d "$data" -D "$new" -U postgres)
if [ -n "$LINK" ]; then upgrade+=(--link); fi
"${upgrade[@]}" --check || { report --check; exit 1; }
# From here on the old cluster can change (--link renames its pg_control).
changing=1
"${upgrade[@]}" || { report ""; exit 1; }
mv "$data" "$keep"
mv "$new" "$data"
if [ -n "$wal" ]; then
  mv "$wal" "$wal_keep"
  mv "$wal_new" "$wal"
  ln -sfn "$wal" "$data/pg_wal"
  ln -sfn "$wal_keep" "$keep/pg_wal"
  echo "WAL: PostgreSQL $TO's is $wal, PostgreSQL $FROM's is kept as $wal_keep"
fi

# pg_upgrade's suggested script updates timescaledb straight to this image's
# default release, past any stop the plan makes; the plan's updates replace it.
if [ -f update_extensions.sql ]; then
  echo "not run, removed: pg_upgrade's update_extensions.sql:"; sed 's/^/    /' update_extensions.sql
  rm -f update_extensions.sql
fi
# Proof of the mode: with --link the new cluster's relation files are hard
# links into the old directory; a copy has none.
echo "relation files sharing an inode with $keep: $(find "$data/base" -type f -links +1 | wc -l)"
echo "upgraded to PostgreSQL $TO; the old data directory is $keep"
kept=()
for d in "$data".pg*; do kept+=("$d"); done
if [ -n "$wal" ]; then for d in "$wal".pg*; do kept+=("$d"); done; fi
echo "old data directories on this disk, to remove once the result is accepted: ${kept[*]}"
