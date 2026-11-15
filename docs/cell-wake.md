# Tokenless cell-side wake (card #20 B1)

`swarph monitor wake-from-inbox --cell opencode --inbox /var/log/swarph-proxy/opencode/monitor/inbox.log --pane opencode:0.0`
reads a proxy monitor's local inbox and submits the fixed line
`check mesh: swarph_dm_unread` to an idle opencode composer. It does not fetch
messages, resolve a gateway, read a token, or make network requests. B2's MCP
proxy transport is separate work. `--once` performs one diagnostic poll.

Run as the actual `cell-opencode` OS account, never the proxy or root. Before
any tmux operation, the command refuses effective UID 0 (exit 77), ownership of
the inbox directory (78), ownership of inbox.log (79), then any account other
than `cell-<--cell>` or an unknown UID (80). The checks repeat before delivery;
the opened inbox inode is checked too. There is no bypass flag. The inbox must
be a regular file in a real directory; unavailable paths defer.

The process uses the cell user's default tmux server. It requires opencode's
actual bottom composer, no running interrupt hint, no permission prompt or
copy mode, and an empty composer or the standing wake alone. It never uses
the generic session-bridge probe. Missing panes, sessions and servers are
deferrals inside the poll loop, not fatal exits. Submission is confirmed by
recapturing the composer; unverified delivery does not advance the cursor.

Set an absolute, single `$STATE_DIRECTORY`. All persistent writes go there:

- `cursor.json` (0600): device/inode, complete-line byte offset, consumed-prefix
  SHA256 and the set of seen DM IDs. Replaced atomically and fsynced only after
  verified wake submission. Duplicate-only progress needs no new wake.
- `heartbeat.json` (0644): only `{ "at": <Unix epoch seconds> }`, refreshed on
  every poll even while the pane is absent. The unit's 0755 state directory
  makes this nonsensitive liveness stamp readable by the lab watchdog/P6.

First start reads from byte zero. A backlog is coalesced into one standing
wake; no DM field is typed. Partial final lines wait for their newline.
Rotation, truncation and copy-truncate/regrow reopen from byte zero, retain
the ID set and raise one standing wake even if the new file is empty or has
only duplicate IDs. Prefix hashing detects same-inode rewrites between polls.

The cursor records submission, not an agent's eventual inbox acknowledgement.
A crash after submission but before the atomic cursor commit can repeat the
standing wake once on restart; persisting before injection could instead lose
mail and is forbidden. Continuous crashes can repeat delivery. The ID set is
retained across rotation, and prefix verification rereads consumed history;
very long-lived logs therefore grow state and polling work. This does not
provide a spool/ack protocol or recover messages removed before first start.

## Unit template (repository only)

`swarph-cell-wake@.service` runs the cell's own
`/home/cell-%i/.local/bin/swarph` as `cell-%i:cell-%i`. It defaults to pane
`%i:0.0`; a drop-in may override `SWARPH_CELL_WAKE_PANE`. It unsets `TMUX` and
`TMUX_TMPDIR`, uses `StateDirectory=swarph-cell-wake/%i`, and limits sockets to
AF_UNIX. No EnvironmentFile, private /tmp, or hidden home directory is used:
the existing cell tmux socket and own install must remain visible.

`Restart=always` recovers failures; exit codes 77–80 prevent restart on unsafe
identity/ownership. The service has NoNewPrivileges, LimitCORE=0 and a strict
read-only system, with the state directory as its writable exception.

Installing, starting and rehearsing the unit belongs to the operator/lab,
not the builder. A rehearsal inbox must be **proxy/lab-owned and cell-readable**,
never cell-owned. Use a scratch proxy-owned inbox and the real cell's session
only with the operator's explicit cutover/rehearsal authority. No template is
installed or started by the tests.
