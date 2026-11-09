# Per-cell proxy deployment (card #20)

These are deployment templates, not an installer or a cutover. Shipping them does
not change live units, users, gateway enforcement, token generations or cell files.
The commander must review the build and separately authorize deployment.

## Trust boundary and files

The cell must never run as the credential uid. Create a dedicated `proxy-<cell>`
account, distinct from the cell account, with no login shell. `cell-<cell>` is the
cell's group. Keep the numeric cell uid stable: `config.json` pins it for every
Unix-socket connection. Do not share one proxy between cells.

The store's `config.json` is a JSON object, not a token-bearing environment file:

```json
{"cell_uid":1001,"gateway_url":"http://127.0.0.1:8788"}
```

Replace the example URL with the operator-chosen gateway. `cell_uid` is mandatory
and must differ from the proxy uid. Optional absolute paths are `token_file`
(defaults beside the configuration as `peer_token`), `socket_path` (Linux default
`/run/swarph-proxy/<cell>.sock`), and `audit_path` (Linux default
`/var/log/swarph-proxy/<cell>.jsonl`). On macOS explicitly set `socket_path` and
`audit_path` to the canonical paths below. Unknown configuration keys, URL userinfo,
query/fragment/base paths, unsafe ownership, or a non-private token store refuse
startup. There is no URL or token environment override. `serve --config` selects
an operator-owned configuration file; `call --socket` selects the local socket.

Required Linux filesystem layout (`<cell>` is a validated cell name; macOS uses
the canonical `/private/var` paths described below):

| Path | Owner / group | Mode |
| --- | --- | --- |
| `/opt/swarph-proxy` and its installed code | root / root | directories 0755; files not group/other writable |
| `/var/lib/swarph-proxy` | root / root | 0755 |
| `/var/lib/swarph-proxy/<cell>` | proxy-cell / proxy-cell | 0700 |
| `/var/lib/swarph-proxy/<cell>/config.json` | proxy-cell / proxy-cell | 0600 |
| `/var/lib/swarph-proxy/<cell>/peer_token` | proxy-cell / proxy-cell | 0600 |
| `/run/swarph-proxy` | root / root | 0755 |
| `/run/swarph-proxy/<cell>.sock` | proxy-cell / cell-cell | 0660 |
| `/var/log/swarph-proxy` | root / root | 0755 |
| `/var/log/swarph-proxy/<cell>.jsonl` | proxy-cell / proxy-cell | 0640 |
| `/var/log/swarph-proxy/<cell>` | proxy-cell / cell-cell | 0750 |
| `/var/log/swarph-proxy/<cell>/monitor` | proxy-cell / cell-cell | 02750 |

Here `proxy-cell` and `cell-cell` mean `proxy-<cell>` and `cell-<cell>`, respectively.
Provision the audit file before starting: the proxy cannot create files in the
root-owned log parent. Never make that parent writable to all proxies or cells.
Every ancestor must be non-cell-writable, including resolved symlink targets.
Do not put monitor state below the 0700 credential HOME: the cell cannot traverse
that HOME even when the child directory is group-readable. The external monitor
directory's setgid bit preserves the cell group; `UMask=0027` creates 0640 logs.
The cell can read `inbox.log` but cannot replace files or insert symlinks there.

Install a reviewed wheel and dependencies into a **root-owned, non-editable**
venv at `/opt/swarph-proxy`, never into a cell pipx environment or `/home`.
Use copied interpreters rather than a symlinked venv executable for the strict
install-path preflight.
Audit the resolved Python interpreter, `pyvenv.cfg`, standard library,
site-packages, `.pth` files and every import-path ancestor as well as the
`ExecStart` path: all installed import components must be root-owned, and none
may be writable by the cell. Do not use an editable install,
cell-owned Python, user site packages, cell-generated modules or shell wrappers.
Both processes use `/opt/swarph-proxy/bin/python -I`; their HOME and working
directory are the private proxy store. Isolated Python ignores the caller's
`PYTHONPATH`, user site and current-directory import injection. It is not a
substitute for filesystem ownership.

Example store config (the address is illustrative; only the operator chooses it):

```json
{"gateway_url":"http://127.0.0.1:8080","cell_uid":12345}
```

The gateway is loaded from this file, not a caller argument or environment.
`peer_token` is the default token filename. Never put a bearer token in a unit,
plist, environment, command argument or deployment transcript. The monitor uses
the same protected config and token file; it cannot accept a gateway override.
Request schemas and the 15-second upstream / 20-second client timeout contract
are documented with the proxy protocol. Clients must not fall back to direct
credentials when the proxy is unavailable.

## Linux: system units and pull-only monitor

The packaged resources are:

- `systemd/swarph-proxy@.service`
- `systemd/swarph-proxy@.socket`
- `systemd/swarph-monitor@.service.d/zzzz-proxy20.conf`

The system manager owns socket creation. It assigns only the socket inode to
`proxy-<cell>:cell-<cell>`; `/run/swarph-proxy` remains root-owned and never
cell-writable. There is deliberately no per-service `RuntimeDirectory=` that
could recursively change ownership of this shared parent. Provision or verify
the parent before activation, including after reboot. Do not run these as user
units. The service consumes the socket inherited from systemd.

Apply the monitor drop-in on the **instance**, at
`/etc/systemd/system/swarph-monitor@<cell>.service.d/zzzz-proxy20.conf`.
An instance-specific legacy drop-in takes precedence over a template-level
drop-in regardless of filename; placing this file only under `@.service.d` is
not sufficient. Its filename sorts after both known live overrides,
`20-tmux-wake-961.conf` and `zz-pilot20.conf`. Remove those two old overrides or
verify the instance's merged configuration resets them. Inspect every additional
drop-in too; a later override is not safe merely because these templates exist.

The override clears inherited environment files, environment values and command
hooks, changes the monitor to `proxy-<cell>`, and executes only the root-installed
`swarph_cli.proxy_monitor` wrapper. The wrapper runs the existing monitor inline
with `--deliver pull --foreground`, protected config and explicit token-file
arguments. It never connects to a cell tmux server or delivers a wake command.
The cell tails the group-readable inbox itself. Do not preserve a tmux sink:
a tmux server can instruct its client to execute as the credential uid.

Both monitor and proxy must show `LimitCORE=0`; the process hardening also disables
dumpability. Before activation inspect the effective `User`, `Group`, HOME,
working directory, every `Exec*` command, environment files, core limit and
writable paths, not just these source templates. Confirm the service and socket
name the same instance. Run the fake-gateway/security tests before a rollout;
SIGSEGV/no-core validation belongs in the target host acceptance check too.

## macOS: commander-installed LaunchDaemons

`launchd/swarph-proxy.plist` and `launchd/swarph-proxy-monitor.plist` are separate
root-managed LaunchDaemon templates. Julien provisions the separate account,
groups, reviewed root-owned install and protected paths; this build does not
create or install them. Replace `__CELL__` with the same validated cell name in
both plists, and keep the installed plists root-owned and non-cell-writable.
Do not use the older ordinary `swarph-monitor.plist` for the credential uid.

Use canonical paths, not macOS's `/var` symlink: the plists set HOME and working
directory to `/private/var/lib/swarph-proxy/<cell>` and the monitor state to
`/private/var/log/swarph-proxy/<cell>/monitor`. The configuration is
`/private/var/lib/swarph-proxy/<cell>/config.json`, with `peer_token` beside it.
Provision the same ownership/modes as the Linux layout under `/private/var/lib`
and `/private/var/log`; the audit file is
`/private/var/log/swarph-proxy/<cell>.jsonl`. This avoids accepting symlinked
ancestors in the credential boundary. The install remains `/opt/swarph-proxy`
with copied Python interpreters and root-owned interpreter, standard library,
site-packages and import-path components. No template provisions these files.

Both new plists set soft **and** hard Core limits to zero and use isolated Python
from `/opt/swarph-proxy`. The runtime uses macOS peer-uid checking and anti-attach
hardening rather than claiming Linux `SO_PEERCRED` or `PR_SET_DUMPABLE` support.
The macOS store must specify a writable-by-proxy `socket_path`, for example
`/private/var/run/swarph-proxy/<cell>/proxy.sock`; the client must use that same
path. Provision `/private/var/run/swarph-proxy` as root:root 0755 and the per-cell
child as `proxy-<cell>:cell-<cell>` 0750. The cell can traverse but cannot modify
the socket parent; the proxy can bind there without root. The plists use the
cell group as the proxy's primary group so its newly bound 0660 socket is
cell-accessible. No ancestor may be cell-writable. launchd does not provide this
template with a systemd activation socket. Verify peer rejection, socket group, anti-attach
and SIGSEGV/no-core behavior on the actual Mac before enabling a pilot; Linux
tests cannot establish those macOS runtime guarantees.

Platform references: [Apple getpeereid(3)](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html),
[Apple ptrace(2)](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/ptrace.2.html),
and [Linux PR_SET_DUMPABLE](https://www.man7.org/linux/man-pages/man2/PR_SET_DUMPABLE.2const.html).
`PT_DENY_ATTACH` denies future tracing and exits if already traced; do not run the
credential process under a debugger. It is not a replacement for separate Unix
users, protected files, or launchd's zero core limits.

## Cutover gate and order (operator only)

This checklist describes the approved contract; it is not authorization to run it.

1. Inspect `/migration-status` and revoked-token warning logs. Enforcement is
   gateway-wide: ensure no other cell still relies on a revoked generation.
2. With revocation enforcement on, revoke this cell's current generation.
3. Register as the proxy account, writing the new response directly into its
   protected token store. Do not register as the cell or write through its HOME.
   Registration only mints a new generation after the latest one is revoked.
4. Clear `MESH_GATEWAY_TOKEN` from cell service environment, profiles and tmux
   global environment; remove the old cell token file, legacy token path and
   `~/lab-state/opencode.peer_token.moved-to-cell-opencode`. Check that the cell's
   `~/.swarph/secrets.toml` is absent. These revoked files are not rollback tokens.
5. Remove or supersede **both** legacy monitor drop-ins, inspect the complete
   effective units, then validate proxy-only access and pull-only inbox reading.

Rotation without enforcement is not cutover. Root and the `ubuntu` administrator
with passwordless sudo remain outside this isolation boundary. A proxy failure
is an ordinary tool error; never recover it by restoring a token to the cell.
