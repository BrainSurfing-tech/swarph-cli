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
#
# CUSTOM STATE DIR (#132): a cell whose monitor state does NOT live in the
# default ~/swarph_state/<self>/mesh-sidecar MUST export SWARPH_STATE_DIR (or
# pass the path as $1). Without it this script reads the empty default dir,
# concludes "not running", and starts a DUPLICATE monitor replaying from id 0
# against the healthy one.
set -u
# >>> #360: NO PEER DEFAULT. An unset identity is UNKNOWN, not lab-ovh. <<<
# `${SWARPH_SELF:-lab-ovh}` took the state UNKNOWN and rendered it as a
# DETERMINATE, SPECIFIC, WRONG PEER — so a cell that never set the variable
# started a monitor AS lab-ovh, draining lab's DMs and marking them read.
# A wrong identity behaves identically to a right one until an audit, which is
# why this survived: it fails in the reassuring direction. On a mesh that
# hardens caller-binding with 403s, a default that makes you someone else is the
# same hole with better manners. (Found on a peer box by gridiron, confirmed by
# science-claude, msgs 22344/22346.)
#
# THE FIX HONOURS BOTH CONSTRAINTS, WHICH LOOK OPPOSED AND ARE NOT. This script
# promises above that it NEVER FAILS THE CALLER — a hook that can block a
# session is worse than the deafness it prevents — so `${SWARPH_SELF:?...}`,
# the obvious fix, is WRONG HERE: it exits non-zero and hands a SessionStart
# hook the power to wedge a session.
# REFUSE THE ACTION, NOT THE CALLER. Say so loudly, do nothing, exit 0.
SELF="${SWARPH_SELF:-}"
if [ -z "$SELF" ]; then
  echo "[monitor] SWARPH_SELF is unset — REFUSING to start a monitor under a" \
       "guessed identity. Set it in the LAST process boundary before the agent" \
       "(the per-cell launcher; a systemd Environment= is two boundaries too" \
       "early for a tmux-hosted cell), or pass --as <peer> explicitly."
  exit 0
fi
SW="$(command -v swarph || echo /home/ubuntu/.local/bin/swarph)"
[ -x "$SW" ] || { echo "[monitor] swarph not found — skipping"; exit 0; }

# >>> #132: PASS THE STATE DIR THROUGH, OR THIS SCRIPT INVENTS A SECOND CELL. <<<
# All three call sites below used to read the CLI DEFAULT state dir. A cell with
# a custom layout (droplet: /var/lib/swarph/droplet-monitor) got "not running"
# from status against the EMPTY default path, and this script then started a
# SECOND monitor replaying from id 0 — against a monitor that was healthy the
# entire time. The helper written to guarantee one supervised monitor created
# the duplicate on exactly the boxes that customised their layout.
# Honour SWARPH_STATE_DIR, or an explicit positional arg (the arg wins: an
# operator typing a path is being more specific than the environment).
# `${SD[@]+...}` is the bash-3.2-safe empty-array idiom — `set -u` plus
# "${SD[@]}" on an empty array is an unbound-variable death on old bash, and
# this script's one promise is that it NEVER fails the caller.
STATE_DIR="${1:-${SWARPH_STATE_DIR:-}}"
SD=()
[ -n "$STATE_DIR" ] && SD=(--state-dir "$STATE_DIR")

"$SW" monitor status --as "$SELF" ${SD[@]+"${SD[@]}"} >/dev/null 2>&1
case $? in
  0) ;;                                   # running, nothing pending
  1) ;;                                   # running, DMs pending — reported below
  *) # not running (2) or unknown: start it. Idempotent, so a race is harmless.
     if "$SW" monitor start --as "$SELF" --deliver pull ${SD[@]+"${SD[@]}"} >/tmp/.swarph-monitor-start.$$ 2>&1; then
       echo "[monitor] started (--deliver pull)"
     else
       # LOUD, not silent: an auto-start that fails quietly rebuilds the exact
       # deafness this exists to remove.
       echo "[monitor] AUTO-START FAILED — you are NOT being watched:"
       sed 's/^/[monitor]   /' /tmp/.swarph-monitor-start.$$
     fi
     rm -f /tmp/.swarph-monitor-start.$$ ;;
esac

OUT="$("$SW" monitor status --as "$SELF" ${SD[@]+"${SD[@]}"} --brief 2>&1)"; RC=$?
[ -n "$OUT" ] && echo "[monitor] $OUT"
[ "$RC" = 1 ] && echo "[monitor] read them: swarph mesh inbox --as $SELF"
exit 0
