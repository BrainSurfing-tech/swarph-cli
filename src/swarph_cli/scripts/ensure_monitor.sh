#!/usr/bin/env bash
# swarph-ensure-monitor — the commander's resilient silent mode, 2026-07-26.
#
#   "couldn't the sidecar verify & run the monitor for a fully workable silent
#    mode 'you have dm' using swarph monitor status & start?"
#
# Card #122 built the primitives (idempotent start; status exit 0/1/2) and nothing
# called them. This is the caller. Safe to run unconditionally from a SessionStart
# hook, a cron, or a shell rc.
#
# WHY PULL AND NOT PUSH: every push sink's liveness is a precondition for hearing
# anything, so a dead tmux pane is indistinguishable from an empty mesh. This runs
# INSIDE the cell, one layer above any sink, so it cannot die with one.
#
# NEVER FAILS THE CALLER. A hook that can block a session is worse than the
# deafness it prevents — exit 0 unconditionally, report on stdout.
set -u
SELF="${SWARPH_SELF:-lab-ovh}"
SW="$(command -v swarph || echo /home/ubuntu/.local/bin/swarph)"
[ -x "$SW" ] || { echo "[monitor] swarph not found — skipping"; exit 0; }

"$SW" monitor status --as "$SELF" >/dev/null 2>&1
case $? in
  0) ;;                                   # running, nothing pending
  1) ;;                                   # running, DMs pending — reported below
  *) # not running (2) or unknown: start it. Idempotent, so a race is harmless.
     if "$SW" monitor start --as "$SELF" --deliver pull >/tmp/.swarph-monitor-start.$$ 2>&1; then
       echo "[monitor] started (--deliver pull)"
     else
       # LOUD, not silent: an auto-start that fails quietly rebuilds the exact
       # deafness this exists to remove.
       echo "[monitor] AUTO-START FAILED — you are NOT being watched:"
       sed 's/^/[monitor]   /' /tmp/.swarph-monitor-start.$$
     fi
     rm -f /tmp/.swarph-monitor-start.$$ ;;
esac

OUT="$("$SW" monitor status --as "$SELF" --brief 2>&1)"; RC=$?
[ -n "$OUT" ] && echo "[monitor] $OUT"
[ "$RC" = 1 ] && echo "[monitor] read them: swarph mesh inbox --as $SELF"
exit 0
