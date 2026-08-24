#!/bin/bash
# Codex PostCompact emits the native post-compaction envelope directly.
@PYTHON@ -m swarph_cli postcompact-hook-output 2>/dev/null || echo '{}'
