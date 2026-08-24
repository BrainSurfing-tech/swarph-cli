#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — grok PostToolUse hook,
# matcher search_replace|write|Write|Edit|MultiEdit|NotebookEdit.
# Grok stdin is camelCase (toolInput); Write/Edit alias to search_replace /
# write. Also accept Claude snake_case tool_input.file_path so a shared
# envelope still emits. Failure-mode invariant: exit 0 on every path.
input=$(cat)
printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read().lstrip("\ufeff"))
    ti = d.get("tool_input") or d.get("toolInput") or {}
    fp = ti.get("file_path") or ti.get("path") or ti.get("target_file") or ""
except Exception:
    fp = ""
print(json.dumps({"tool_input": {"file_path": fp}}) if fp else "{}")
' | @ENV_PREFIX@@PYTHON@ -m swarph_cli memory-emit-hook >/dev/null 2>&1
echo '{}'
exit 0
