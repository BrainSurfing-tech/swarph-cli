#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — claude PostToolUse hook,
# matcher Write|Edit|MultiEdit|NotebookEdit: emit-on-write for memory files.
# Claude's envelope is already the shape memory-emit-hook reads — no adapter.
# Failure-mode invariant: exit 0 on every path.
input=$(cat)
printf '%s' "$input" | @ENV_PREFIX@@PYTHON@ -m swarph_cli memory-emit-hook >/dev/null 2>&1
echo '{}'
exit 0
