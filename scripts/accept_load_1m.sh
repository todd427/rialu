#!/usr/bin/env bash
# Acceptance check for docs/cc-brief-teas-load-1m.md.
#
#   scripts/accept_load_1m.sh            # default tolerance 1.0
#   TOL=0.5 scripts/accept_load_1m.sh
#
# For every online machine: the load figure in `teas --list` must be a real
# number and agree with that machine's own /proc/loadavg. Load moves between
# the two reads, so agreement means "within TOL", not equal.
# Also: rose's row is still plain "offline"; lava (if up) reports a load
# although it has no CPU temperature.
set -u

TOL="${TOL:-1.0}"
ONLINE="daisy iris lily poppy tulip"
HOST="$(hostname -s | tr 'A-Z' 'a-z')"
pass=0; fail=0; skip=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }
skp()  { printf '  \033[33mSKIP\033[0m %s\n' "$*"; skip=$((skip+1)); }

command -v teas >/dev/null || { echo "teas not on PATH"; exit 2; }

# Plain text, no colour codes.
LIST="$(teas --list 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
echo "$LIST"; echo

row() { printf '%s\n' "$LIST" | awk -v m="$1" '$2==m {print; exit}'; }

# Machine's own 1-minute load, read as close to the teas read as possible.
local_load() {
  if [ "$1" = "$HOST" ]; then
    cut -d' ' -f1 /proc/loadavg
  else
    timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=5 "$1" 'cut -d" " -f1 /proc/loadavg' 2>/dev/null
  fi
}

echo "== online machines: teas --list load matches /proc/loadavg (tol $TOL)"
for m in $ONLINE; do
  r="$(row "$m")"
  if [ -z "$r" ]; then bad "$m: no row in teas --list"; continue; fi
  case "$r" in *offline*) bad "$m: shown offline"; continue;; esac
  shown="$(printf '%s\n' "$r" | sed -n 's/.*load[[:space:]]*\([0-9][0-9.]*\)[[:space:]]*$/\1/p')"
  if [ -z "$shown" ]; then bad "$m: no numeric load in row (got: ${r##*  })"; continue; fi
  actual="$(local_load "$m")"
  if [ -z "$actual" ]; then skp "$m: shows $shown but could not read its /proc/loadavg"; continue; fi
  if awk -v a="$shown" -v b="$actual" -v t="$TOL" 'BEGIN{d=a-b; if(d<0)d=-d; exit !(d<=t)}'; then
    ok "$m: teas $shown vs host $actual"
  else
    bad "$m: teas $shown vs host $actual (differs by more than $TOL)"
  fi
done

echo; echo "== rose: offline row unchanged"
r="$(row rose)"
if [ -z "$r" ]; then bad "rose: no row"
else
  case "$r" in
    *offline*) case "$r" in *load*) bad "rose: offline row grew a load column: $r";; *) ok "rose: plain offline row";; esac;;
    *) skp "rose is online now; nothing to compare";;
  esac
fi

echo; echo "== lava: reports a load though it has no CPU temp"
r="$(row lava)"
case "$r" in
  "")        bad "lava: no row";;
  *offline*) skp "lava is offline (laptop, intermittent by design)";;
  *)
    printf '%s\n' "$r" | grep -Eq 'cpu[[:space:]]+—' || skp "lava has a CPU temp now; the no-temp case is not exercised"
    if printf '%s\n' "$r" | grep -Eq 'load[[:space:]]+[0-9]'; then ok "lava: $r"
    else bad "lava: no numeric load: $r"; fi;;
esac

echo; echo "pass=$pass fail=$fail skip=$skip"
[ "$fail" -eq 0 ]
