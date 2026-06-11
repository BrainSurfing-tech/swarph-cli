# Sidecar reference deployment

Production-deployment reference for `swarph mesh sidecar` + its companions, as
run on the **gpu-wsl** cell (the reference build lab tracked for the in-tmux-spawn
pattern). The sidecar **source** is `src/swarph_cli/commands/mesh.py`
(`swarph mesh sidecar`); this directory is the **operator wiring** — the systemd
units + the discipline that make it a self-healing service, none of which lived
in the repo before.

## What the sidecar does

Every `--poll-seconds` it: drains this peer's mesh-gateway inbox (peer-token-direct
auth), advances a transactional cursor + `inbox.log` under
`~/swarph_state/<PEER>/`, and on **new mail** runs `tmux send-keys "check mesh"`
into a pinned tmux session — waking a **live claude** to process the DM. It is the
post-C5 replacement for the shared-token `swarph daemon`.

## The three-unit stack

| Unit | Role |
|---|---|
| `swarph-mesh-sidecar.service` | DM drain + tmux-wake (peer-token-direct) |
| `claude-tmux.service` + `launch-cell.sh` | durable tmux session hosting a **live** claude at boot (the wake *target*) |
| `swarph-watchdog.{service,timer}` (already in `src/swarph_cli/systemd/`) | recovery: if the cursor goes stale AND no claude process, A1 `send-keys` wake → A2 `swarph spawn` respawn |

The sidecar is the doorbell; claude-tmux is who answers it; the watchdog is the
backstop if the doorbell rings into a dark house.

## Install (no root — systemd USER services)

```bash
# 1. substitute placeholders in the unit files: <PEER> <HOME> <GATEWAY> <TMUX>
# 2. drop them in
cp swarph-mesh-sidecar.service claude-tmux.service ~/.config/systemd/user/
install -m755 launch-cell.sh ~/.config/swarph/launch-<PEER>.sh
# 3. enable
systemctl --user daemon-reload
systemctl --user enable --now claude-tmux.service swarph-mesh-sidecar.service
# 4. watchdog (from src/swarph_cli/systemd/): swarph watchdog --install-service
```

## cell.yaml pins the watchdog needs (F4)

`~/.config/swarph/cells/<PEER>.yaml` must pin where the cursor lives and which
tmux session to wake — as **top-level keys**:

```yaml
cursor_path: /home/<USER>/swarph_state/<PEER>/cursor.json
tmux_session: <TMUX>
```

> ⚠️ **F4 gotcha:** put these at the **top level**, NOT under an `extra:` block.
> The `Cell` schema's catch-all double-nests an explicit `extra:` map into
> `Cell.extra['extra']`, so `extra.get('cursor_path')` resolves `None` and the
> watchdog silently falls back to `/tmp/lab-claude-cursor.json` + `tmux=<role>`.
> (Fixed at the type layer in swarph-shared #11; top-level keys work on every
> version.)

## Hard-won discipline (read before deploying)

- **Token-less env, by design.** Do NOT set `MESH_GATEWAY_TOKEN` (or an
  EnvironmentFile that does) on the sidecar unit. With no env token,
  `mesh._resolve_token` falls through to `~/.config/swarph/<PEER>.peer_token`
  (0600). A stray env token re-introduces the retired shared-token coupling.
- **Wake a live agent, not a shell.** A bare `tmux new-session` leaves the wake
  target running `bash` → `send-keys "check mesh"` lands in an idle shell and
  does nothing. `claude-tmux.service` + `launch-cell.sh` host a real `swarph spawn`
  in the pane so wakes do work. (Empirically proven: DM arrives → send-keys →
  live claude drains + acts.)
- **One claude per session-UUID.** Never run a second console claude resuming the
  same session-id while the in-tmux one is live — two resumes of one UUID kill the
  tmux server. Pick in-tmux OR console, not both.
- **Hostname resolution can flake.** Units use `<GATEWAY>` by hostname; if MagicDNS
  drops the gateway name (e.g. after a VPN/tailscale re-auth), the sidecar polls
  fail until it resolves again. The gateway's tailnet IP is stable — fall back to
  it if the hostname is unreliable on your box.
- **Liveness check:** `systemctl --user status swarph-mesh-sidecar` +
  `journalctl --user -u swarph-mesh-sidecar` (watch for `wake`/`401`/`fail`).
  Cursor at `~/swarph_state/<PEER>/cursor.json` should advance on traffic.

## Provenance

Authored from the gpu-wsl cell (WSL2/systemd, peer-token-direct) during the
post-C5 sidecar migration + in-tmux-spawn proof. Shared so any Linux/systemd peer
can stand up the same self-healing DM loop from a template instead of
reverse-engineering it.
