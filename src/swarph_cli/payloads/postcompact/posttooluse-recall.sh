#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — cursor postToolUse hook.
# First tool call after a compaction: inject the 7-day timeline recall as
# additional_context. The flag file is the handshake with preCompact.
# Failure-mode invariant: exit 0 on every path — worst case is no recall.
input=$(cat)
conv=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().lstrip("\ufeff")).get("conversation_id",""))' 2>/dev/null)
flag="${TMPDIR:-/tmp}/cursor-compact-pending-$conv"
if [ -n "$conv" ] && [ -f "$flag" ]; then
  rm -f "$flag"
  @PYTHON@ -m swarph_cli postcompact-hook-output 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    ctx = (d.get("hookSpecificOutput") or {}).get("additionalContext", "")
except Exception:
    ctx = ""
print(json.dumps({"additional_context": ctx} if ctx else {}))'
  exit 0
fi
echo '{}'
exit 0
