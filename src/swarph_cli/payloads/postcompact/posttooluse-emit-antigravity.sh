#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — antigravity PostToolUse hook,
# matcher replace_file_content|write_to_file|Write|Edit: emit-on-write for memory files.
# The inner python is the shape adapter for Antigravity tool arguments.
# Failure-mode invariant: exit 0 on every path.
input=$(cat)
printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read().lstrip("\ufeff"))
    tc = d.get("toolCall") or {}
    args = tc.get("args") or d.get("tool_input") or {}
    fp = args.get("TargetFile") or args.get("file_path") or args.get("path") or args.get("target_file") or ""
except Exception:
    fp = ""
print(json.dumps({"tool_input": {"file_path": fp}}) if fp else "{}")
' | @ENV_PREFIX@@PYTHON@ -m swarph_cli memory-emit-hook >/dev/null 2>&1
echo '{}'
exit 0
