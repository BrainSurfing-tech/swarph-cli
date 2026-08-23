#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — claude SessionStart hook.
# Claude fires SessionStart with source="compact" on the first post-compact
# turn, so no flag-file handshake is needed — the envelope names the moment.
# The recall verb emits hookEventName PostCompact; rewrite it to SessionStart
# (claude validates the event name against the hook that fired).
# Failure-mode invariant: exit 0 on every path — worst case is no recall.
input=$(cat)
src=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().lstrip("\ufeff")).get("source",""))' 2>/dev/null)
if [ "$src" = "compact" ]; then
  @PYTHON@ -m swarph_cli postcompact-hook-output 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    ctx = (d.get("hookSpecificOutput") or {}).get("additionalContext", "")
except Exception:
    ctx = ""
out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}} if ctx else {}
print(json.dumps(out))'
  exit 0
fi
echo '{}'
exit 0
