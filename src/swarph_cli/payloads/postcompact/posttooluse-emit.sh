#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — cursor postToolUse hook,
# matcher Write|Edit|StrReplace: emit-on-write for memory files.
# The inner python is the shape adapter: cursor Write/Edit carry
# tool_input.path; memory-emit-hook reads claude-shaped tool_input.file_path.
# Failure-mode invariant: exit 0 on every path.
input=$(cat)
printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read().lstrip("\ufeff"))
    ti = d.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("path") or ""
except Exception:
    fp = ""
print(json.dumps({"tool_input": {"file_path": fp}}) if fp else "{}")
' | @ENV_PREFIX@@PYTHON@ -m swarph_cli memory-emit-hook >/dev/null 2>&1
echo '{}'
exit 0
