# Daily jobs: stuck-work sweep + organ triage router (systemd)

The shipped units (`swarph-stuck-sweep.service/.timer`,
`swarph-triage.service/.timer` under `src/swarph_cli/systemd/`) carry
SYSTEM-INSTALL DEFAULTS: `ExecStart=/usr/local/bin/swarph`, no `User=`
(runs as root), logs to `/var/log`. That fits a root install and
nothing else.

## User install (lab reality, card #1073 #1432)

Lab runs both jobs as its own user through a drop-in. Copy this shape —
do not edit the shipped unit, or the next upgrade overwrites it:

```sh
sudo systemctl edit swarph-triage          # or swarph-stuck-sweep
```

Substitute `<USER>` / `<HOME>` for the runner (lab uses its own user
and `~/.local/bin/swarph` — never `/usr/local/bin`, which is absent):

```ini
# /etc/systemd/system/swarph-triage.service.d/override.conf —
# review copy only, do not copy tokens here (there are none to copy:
# the job reads the runner's own credential at start).
[Service]
User=<USER>
ExecStart=
ExecStart=<HOME>/.local/bin/swarph triage --as <USER>
StandardOutput=append:<HOME>/var/log/swarph-triage.log
StandardError=append:<HOME>/var/log/swarph-triage.log
```

Then `sudo systemctl daemon-reload` and enable the matching timer
(`swarph-triage.timer`, `swarph-stuck-sweep.timer`). Verify at the
destination, not from the install command:

```sh
systemctl is-enabled swarph-triage.timer
journalctl -u swarph-triage --since "1 hour ago" | tail -5
```

## Why the shipped default stays root-shaped

Baking one cell's user and paths into the shipped unit would break
every other operator the way a hardcoded gateway did (#578). The
system default plus a documented drop-in keeps the upgrade path clean:
the drop-in survives unit replacement, an edited unit does not.
