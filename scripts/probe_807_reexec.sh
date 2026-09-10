#!/bin/bash
# probe_807_reexec.sh — the can-fail for card #807 / obligation #125.
# Reads DIFFERENTLY on the broken and the fixed unit: records the supervised
# monitors' MainPIDs and the reexec units' states, forces a REAL swarph-cli
# reinstall (pip --user, the tree the monitors load), then polls for up to
# WAIT seconds and reports how many PIDs moved and what the units read.
# Unprivileged: pip --user + systemctl show. Run it BEFORE the fix (expect
# MOVED 0/N and the .path latched) and AFTER (expect MOVED N/N, units clean).
set -uo pipefail
WAIT=${WAIT:-300}
VER=$(swarph --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
units() { systemctl list-units --no-legend 'swarph-monitor@*.service' | awk '{print $1}'; }
pids() { for u in $(units); do echo "$u=$(systemctl show "$u" -p MainPID --value)"; done; }
state() { for u in swarph-monitor-reexec.path swarph-monitor-reexec.service; do
  echo "$u: $(systemctl show "$u" -p ActiveState -p Result -p NRestarts --value | tr '\n' ' ')"; done; }
echo "== BEFORE ($(date -u +%FT%TZ)) swarph-cli $VER"; BEFORE=$(pids); echo "$BEFORE"; state
echo "== reinstall (pip --user --force-reinstall --no-deps swarph-cli==$VER)"
pip install --user --force-reinstall --no-deps "swarph-cli==$VER" >/dev/null 2>&1; echo "pip rc=$?"
t0=$(date +%s)
while :; do
  AFTER=$(pids); moved=0; total=0
  while IFS='=' read -r u p; do total=$((total+1)); b=$(echo "$BEFORE" | grep "^$u=" | cut -d= -f2); [ "$p" != "$b" ] && [ "$p" != "0" ] && moved=$((moved+1)); done <<< "$AFTER"
  [ "$moved" -ge "$total" ] && break
  [ $(( $(date +%s) - t0 )) -ge "$WAIT" ] && break
  sleep 10
done
echo "== AFTER ($(date -u +%FT%TZ), waited $(( $(date +%s) - t0 ))s)"; echo "$AFTER"; state
echo "MOVED $moved/$total"
