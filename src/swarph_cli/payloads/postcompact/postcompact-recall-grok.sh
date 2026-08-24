#!/bin/bash
# swarph postcompact wiring (cards #549/#566) — grok PostCompact hook.
# Grok has a native PostCompact event (compaction trigger matcher: manual|auto).
# Claude's SessionStart source=="compact" is a different envelope; installing
# --harness claude only *may* fire on grok (compat.claude hooks=true scans
# ~/.claude/settings.json) and only if grok ever sets source=compact — which
# the grok docs do not list among SessionStart sources (startup, resume, …).
# postcompact-hook-output already emits hookEventName PostCompact; pass through.
# Failure-mode invariant: exit 0 on every path — worst case is no recall.
input=$(cat)
printf '%s' "$input" | @PYTHON@ -m swarph_cli postcompact-hook-output 2>/dev/null || echo '{}'
exit 0
