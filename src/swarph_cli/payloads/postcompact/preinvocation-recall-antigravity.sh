#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — antigravity PreInvocation hook.
# Injects the 7-day timeline recall as ephemeral context on turn 1.
# Failure-mode invariant: exit 0 on every path — worst case is no recall.
input=$(cat)
inv_num=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().lstrip("\ufeff")).get("invocationNum", 1))' 2>/dev/null)
if [ "$inv_num" = "1" ] || [ -z "$inv_num" ]; then
  @PYTHON@ -m swarph_cli postcompact-hook-output 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    ctx = (d.get("hookSpecificOutput") or {}).get("additionalContext", "")
except Exception:
    ctx = ""
out = {"injectSteps": [{"ephemeralMessage": ctx}]} if ctx else {}
print(json.dumps(out))'
  exit 0
fi
echo '{}'
exit 0
