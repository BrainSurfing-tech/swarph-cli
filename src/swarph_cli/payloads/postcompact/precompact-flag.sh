#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — cursor preCompact hook.
# Sets the pending-flag for this conversation; the postToolUse recall script
# consumes it on the first tool call after the compaction completes.
# Failure-mode invariant: exit 0 on every path — never block the session.
input=$(cat)
conv=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read().lstrip("\ufeff")).get("conversation_id",""))' 2>/dev/null)
[ -n "$conv" ] && touch "${TMPDIR:-/tmp}/cursor-compact-pending-$conv"
echo '{}'
exit 0
