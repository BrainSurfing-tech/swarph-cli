# `swarph memory put` — write-time memory verb (design)

> Card #445. Deferred from #143 (timeline per-entry chunking, shipped 2026-08-13)
> — droplet's own words on the shipped card: "the write-time `swarph memories`
> verb (commander's idea, explicitly out of scope this round)." This spec
> resolves the naming/scope questions that left it deferred.

## Problem

Getting content into gbrain's knowledge hemisphere today has two paths, and
neither is "write it and it's queryable now":

1. `swarph highlight` — appends a line to the shared `TIMELINE.md`. Doesn't
   become semantically queryable until the nightly `swarph-brain-reindex`
   timer (03:30 UTC) runs, and even then only as part of the per-entry
   chunking `_timeline` split #143 shipped.
2. Hand-authored memory page — a cell (or a human operating one) writes a
   `.md` file with YAML frontmatter directly into
   `/home/ubuntu/.claude/projects/-home-ubuntu/memory/`, which the same
   nightly reindex globs and imports.

Both routes wait for the nightly batch. Card #143's own body flagged the gap
its fix didn't close: the *read* side (retrieval) got fixed; the *write*
side did not.

Concretely, this session (2026-08-14): droplet drafted three memory pages
(`epic_cost_accuracy`, `epic_gbrain_retrieval`, `epic_fleet_compact`) and had
to send their full text to lab-ovh over a mesh DM, because droplet has no
write path into the indexed corpus at all — not even a slow one. lab-ovh
hand-wrote the files. That round-trip (compose → DM → hand-write → wait for
reindex) is the thing this verb replaces, for the piece of it that's
solvable without reopening a security boundary (see Non-goals).

## Goals

- A single CLI command that writes a properly-formatted memory page AND
  makes it queryable immediately, without waiting for the nightly reindex.
- Uses the exact frontmatter/body convention already established by hand
  this session (`name`, `description`, `metadata.type`) — output is
  indistinguishable from a hand-authored page to the reindex glob or to
  `gbrain` itself.
- Local-only for this phase: runs where gbrain is co-located (lab-ovh).

## Non-goals (this card)

- **Mesh-wide write access.** `mesh-gateway`'s `POST /memory` proxy
  deliberately restricts remote cells to `get`/`list` — the code comment at
  `server.py:7119` states it plainly: *"the gateway CONSTRUCTS the MCP call
  from an `op` enum — never a caller-supplied tool name — so only the two
  READ tools are reachable; put_page/admin are unreachable by construction."*
  That is an intentional security boundary, not an oversight. Opening a
  write op on the gateway is a separate decision with its own auth/abuse
  surface (rate limits, which cell may write to whose corpus) and belongs on
  its own card once this local phase has proven out. Tracked as the
  **Phase 2 follow-up** below — not specced here, not built here.
- Editing/deleting existing pages (only new-page + full-content overwrite,
  matching `gbrain put`'s own semantics — see Interface).
- Any change to `swarph highlight` or the `TIMELINE.md` path — orthogonal,
  untouched.

## Interface

Extends the existing `swarph memory` verb (`src/swarph_cli/commands/memory.py`)
with a `put` subcommand — not a new module, not a new top-level verb name.
`swarph memory` is already the deterministic-nav verb (`get`/`list`/`links`);
`put` is the deterministic-write counterpart in the same place.

```
swarph memory put <slug> [--content TEXT] [--type TYPE] [< file.md]
```

- `<slug>`: required, matches `gbrain put`'s own slug argument.
- `--content`: full markdown including YAML frontmatter. If omitted, read
  from stdin (mirrors `gbrain put <slug> [< file.md]`'s own two input
  modes, so a caller already piping a file to `gbrain put` can pipe the same
  way to `swarph memory put`).
- `--type`: convenience flag that, when set and `--content`/stdin has no
  `metadata.type` already, stamps it into the frontmatter before writing —
  saves hand-authoring the YAML block for the common case (a one-line
  memory with no cross-links). Ignored if the content already carries a
  `metadata.type`.
- No `--force`/`--overwrite` flag: `put` always writes/overwrites the given
  slug, matching `gbrain put`'s own semantics (`Write/update a page`). A
  caller who wants to avoid clobbering an existing page runs
  `swarph memory get <slug>` first — that check is the caller's job, not
  this verb's.

## Behavior

On invocation:

1. Resolve the calling cell's own memory dir — same resolution `swarph
   highlight` uses for its timeline dir (`SWARPH_TIMELINE_DIR` pattern):
   `--memory-dir` flag > `SWARPH_MEMORY_DIR` env > the reindex script's
   documented default (`/home/ubuntu/.claude/projects/-home-ubuntu/memory`
   on lab-ovh). **If this doesn't resolve to a directory the local
   `swarph-brain-reindex.sh` actually globs, fail loudly before writing
   anything** — a page written somewhere the nightly reindex never looks is
   worse than no page (indistinguishable from success, never actually
   indexed).
2. Write the `.md` file to that directory (slug → `<slug>.md`, same as every
   hand-authored page this session produced). This is the durable,
   git-independent source of truth — exactly what's there today, just
   written by a command instead of by hand.
3. Call `gbrain put <slug> --content <the same content>` inside a stop/start
   window, reusing the exact pattern `swarph-brain-reindex.sh` already runs
   nightly and proved safe again this morning (2026-08-14 03:30 UTC, card
   #143's C1 fix verified live):
   ```
   systemctl stop swarph-brain
   sleep 2
   sudo -u swarphbrain bash -lc '... gbrain put <slug> --content ... ...'
   systemctl start swarph-brain
   sleep 3
   ```
   This step requires the CLI process to have `sudo systemctl stop/start
   swarph-brain` and `sudo -u swarphbrain` rights — the same rights the
   reindex script's systemd unit already runs with. **This is why Phase 1 is
   local-only**: those rights exist on lab-ovh (where gbrain is
   co-located) and nowhere else in the mesh.
4. On success, print the same `logged -> <slug> (immediate)` shape
   `swarph highlight` uses (`logged -> TIMELINE.md @ <ts>`), so the two
   verbs read consistently at a glance.
5. **If step 3 fails after step 2 succeeded** (`gbrain put` errors, or the
   stop/start window doesn't come back healthy): do NOT roll back the file
   written in step 2. Print a clear warning that the page exists locally and
   will be picked up by the next nightly reindex, with the exact command to
   verify once resolved (`swarph memory get <slug>`). This degrades to
   exactly today's existing hand-authored-page behavior — a real regression
   is impossible, worst case is "as slow as before."
6. **Remote invocation** (a cell without local root, e.g. droplet, gpu-wsl):
   step 1's directory resolution fails against reality (the resolved path
   isn't a directory reachable from that cell's filesystem, or `sudo
   systemctl stop swarph-brain` isn't available) — fail with a clear message
   naming lab-ovh as the current write path, not a silent no-op or a
   confusing permissions traceback.

## Operational safety

`gbrain`'s PGLite backend is single-writer; the existing nightly reindex
script's own comments are explicit that a CLI call must never run while the
serving process holds the lock (`feedback_gbrain_pglite_ops_hazards`). This
was independently reconfirmed today: an ad hoc `gbrain get` while
`swarph-brain.service` was live-serving timed out waiting for the lock; the
reindex script avoids this by stopping the service first. `swarph memory
put` reuses that exact proven window rather than attempting a live-write
shortcut — no new operational pattern, no new failure mode.

**Tradeoff, stated plainly for the record:** every `put` briefly takes
`swarph-brain` offline mesh-wide (~5 seconds, matching tonight's reindex
window) — no cell can `brain-ask` or `swarph memory get/list` during that
window. At today's volume (a handful of memory-page writes per day, per this
session's own usage) this is a non-issue. If `swarph memory put` usage grows
enough that overlapping/frequent writes cause noticeable read disruption,
that's a signal to revisit — not a reason to avoid shipping this phase.

## Testing

- Unit: argument parsing (`--content` vs stdin, `--type` stamping behavior,
  missing-slug error), directory resolution fallback chain, and the
  degrade-to-file-only path on a simulated `gbrain put` failure — all
  mockable without touching the live gbrain instance.
- Integration (manual, one-time, against the live lab-ovh gbrain instance,
  run by whoever implements this): write a real page with a disposable slug
  (e.g. `_swarph_memory_put_smoke_test`), confirm `swarph memory get
  <slug>` returns it immediately (no reindex wait), confirm the `.md` file
  exists in the memory dir, then delete the test page via `gbrain delete`
  (mirroring the C1 eviction pattern #143 already proved) so it doesn't
  pollute the corpus permanently.

## Phase 2 (future card, not this one)

Mesh-wide write: add a `put` op to `mesh-gateway`'s `POST /memory` proxy
(`_MEMORY_TOOL_BY_OP`), gated behind whatever auth/abuse decisions that
requires (which cells may write, to whose corpus, rate limits). Graduation-
register style — dated and held by a named owner once actually opened, not
a silent default. Do not build as part of this card.
