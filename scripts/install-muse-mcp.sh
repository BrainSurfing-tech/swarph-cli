#!/usr/bin/env bash
# install-muse-mcp.sh <cell> [gateway-url]: register swarph's MCP tool server in Muse.
#
# Muse starts stdio servers with only a fixed allowlist of the environment
# (HOME, PATH, USER, LANG, TERM...) plus the entry's literal `env` map, and it
# ignores `env_vars` in settings (although `muse mcp add --env-var` writes it).
# So the cell name and gateway URL are written as literal values. Neither is a
# secret: the token stays in ~/.config/swarph/<cell>.peer_token, found via HOME,
# and a gateway URL carrying credentials (@, ?, #) is refused.
# shortcut: per-cell identity in the user's settings file (card #20); fine while
# one Muse cell runs per Unix user, revisit when Muse forwards variables by name.
#
# "mode": "optional" keeps a failed swarph start from blocking Muse's startup.
# Idempotent: the same entry is left alone (exit 0); a different one is
# refused (exit 1). Effective at the next Muse launch. Undo: muse mcp remove swarph
set -euo pipefail

CELL=${1:?usage: install-muse-mcp.sh <cell-name> [gateway-url]}
GATEWAY=${2:-${MESH_GATEWAY_URL:-}}
MUSE=${MUSE:-muse}
NAME=swarph
SETTINGS=${XDG_CONFIG_HOME:-$HOME/.config}/muse/settings.json  # where Muse itself reads

# ASCII-only checks in Python, so the shell's locale cannot widen [a-z].
python3 -I -c 'import re,sys
cell, gw = sys.argv[1], sys.argv[2]
if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}[a-z0-9]", cell, re.ASCII):
    sys.exit("install-muse-mcp: invalid cell name (a-z, 0-9, -, at most 64): " + repr(cell))
if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]_-]+(/[A-Za-z0-9._~/-]*)?", gw, re.ASCII):
    sys.exit("install-muse-mcp: give an http(s) gateway URL without user, password, query or fragment, as the second argument or MESH_GATEWAY_URL")' "$CELL" "$GATEWAY"

SWARPH_BIN=$(command -v swarph) || { echo "install-muse-mcp: swarph is not on PATH" >&2; exit 1; }
SWARPH_BIN=$(python3 -I -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$SWARPH_BIN")

ENTRY=$(python3 -I -c 'import json,sys; print(json.dumps({"type":"stdio","command":sys.argv[1],"args":["mcp-server"],"env":{"SWARPH_SELF":sys.argv[2],"MESH_GATEWAY_URL":sys.argv[3]},"mode":"optional"}))' "$SWARPH_BIN" "$CELL" "$GATEWAY")

if "$MUSE" mcp get "$NAME" --json >/dev/null 2>&1; then
  # `mcp get` redacts env values, so compare against the settings file itself.
  if python3 -I -c 'import json,sys
try:
    e = (json.load(open(sys.argv[1])).get("mcpServers") or {}).get(sys.argv[3]) or {}
except (OSError, ValueError, AttributeError):
    sys.exit(1)
w = json.loads(sys.argv[2])
same = all(e.get(k) == w[k] for k in ("command", "args", "env", "mode")) and e.get("enabled", True) is not False
sys.exit(0 if same else 1)' "$SETTINGS" "$ENTRY" "$NAME"; then
    echo "install-muse-mcp: '$NAME' already registered for $CELL at $GATEWAY; nothing to do"
    exit 0
  fi
  echo "install-muse-mcp: an MCP server named '$NAME' exists with other settings; not replacing it." >&2
  echo "Remove it first with: $MUSE mcp remove $NAME" >&2
  exit 1
fi

"$MUSE" mcp add-json "$NAME" "$ENTRY" --user
echo "install-muse-mcp: registered '$NAME' for $CELL; it starts with the next Muse launch"
