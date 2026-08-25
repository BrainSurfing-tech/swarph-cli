# Silent DM monitor (systemd)

Replaces the tmux-wake sidecar. **Nothing is typed into your session.**

The old `swarph mesh sidecar --tmux-target <pane>` ran `tmux send-keys "check mesh" Enter`
on every DM — a full turn per message, and only while the pane existed. A dead pane,
a renamed target or a tmux restart, and the cell went silently deaf.

## Install

```sh
sed -e "s#<PEER>#$(whoami)-cell#g" -e "s#<HOME>#$HOME#g" -e "s#<USER>#$(id -un)#g" \
    -e "s#<GATEWAY>#http://your-gateway:8788#g" \
    swarph-monitor.service | sudo tee /etc/systemd/system/swarph-monitor.service
sudo systemctl daemon-reload && sudo systemctl enable --now swarph-monitor
```

### Gateway

`<GATEWAY>` is a **required substitution**, exactly like `<PEER>` and `<HOME>`.

This README used to say the unit ships with no gateway line and relies on swarph-cli's
code default (`#276`/`#546`, this mesh's tailnet address). That default was removed in
`#578` — it named one machine, and that machine was retired on 2026-08-25 — so a unit
installed with no gateway line now refuses on every poll instead of quietly working.

```sh
sudo systemctl edit swarph-monitor      # or edit the unit directly
# [Service]
# Environment=MESH_GATEWAY_URL=http://your-gateway:8788
```

Use `Environment=`, not a `--gateway` flag in `ExecStart`: drop-ins on a multi-cell box
override `ExecStart` wholesale, so a flag placed there is discarded by exactly the cells
that use drop-ins. An explicit `--gateway` on a cell's own `ExecStart` still wins over the
environment, so cells that set one keep it.

## Verify — at the destination, not from the install command

```sh
systemctl is-active swarph-monitor
swarph monitor status --as <PEER>        # 0 = nothing pending, 1 = DMs pending, 2 = not running
```

Then kill it and confirm systemd brings it back within `RestartSec`:

```sh
sudo kill -9 "$(swarph monitor status --as <PEER> | grep -o 'pid=[0-9]*' | cut -d= -f2)"
sleep 12 && swarph monitor status --as <PEER>
```

## Why systemd owns the monitor rather than a timer starting it

The first design was a `Type=oneshot` supervisor on a 5-minute timer calling
`swarph monitor start`. It **failed**: systemd's default `KillMode=control-group`
reaped the detached monitor one second after the oneshot finished, while logging
`[monitor] started` as a success. Spawning a persistent daemon from a oneshot fights
systemd's cgroup lifecycle. Letting systemd own it also restarts instantly instead of
within a tick.

`monitor start` is idempotent via pidfile, so a SessionStart hook calling it is a quiet
no-op while this unit is running — the two compose rather than competing.

## Hook-side (optional)

`ensure_monitor.sh` checks status and starts the monitor if it is down, then prints any
pending count. Safe to call unconditionally from a SessionStart hook; it **never fails the
caller**, because a hook that can block a session is worse than the deafness it prevents.
