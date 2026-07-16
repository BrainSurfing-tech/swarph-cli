# swarph memory graph traversal — Design Spec

**Board card #42** · Sub-project B of the OKF traversal brain
([parent spec](../../../../swarph-shim/docs/superpowers/specs/2026-07-15-okf-traversal-brain-design.md))
**Date:** 2026-07-16 · **Target:** swarph-cli (public PyPI, stdlib-only)

## Thesis

`swarph memory links` gains **backlinks** and **multi-hop, directional** graph
traversal over the OKF knowledge corpus. The graph is derived **file-native**
from page *bodies* (the `[[links]]` an author actually wrote), using gbrain only
as the page store (`get_page`/`list_pages` over MCP) and the shared
`okf_links.parse_okf_links` parser as the edge extractor. No gbrain change, no
brain-data mutation, no deploy gate.

## Why file-native (the load-bearing finding)

The gbrain graph tools (`traverse_graph`, `get_backlinks`, `get_links`) are
already live MCP tools on the prod brain — but lab's brain has **`link_count: 0`**.
The `[[links]]` in all 198 pages were never reconciled into gbrain's edge table
(gbrain's `markdown`/`wikilink-resolved` reconciliation never ran on our corpus).
So the links exist **only in the page bodies**. Deriving the graph from bodies
therefore both (a) works today against the real corpus, and (b) matches the
parent spec's "file-native for shippability" thesis — it ships to any gbrain
regardless of whether its edge index is populated.

Populating gbrain's edge index is real, mesh-wide value (it would light up
`traverse_graph`/`get_backlinks` for brain-ask and the future walker too), but
it is a **separate, gated, brain-health task** — see *Decoupled follow-up* below.
`traverse_graph` remains a documented optional fast-path for a future revision
once edges exist; it is out of scope here.

## Scope

**In scope (this spec):**
1. Migrate `swarph memory`'s link parsing to the shared `okf_links.parse_okf_links`
   (retire the weaker local `parse_links`; closes the duplication the timeline
   sub-project explicitly deferred).
2. File-native traversal engine in `memory.py`: forward BFS, reverse (backlinks)
   index, directional + depth-bounded, cycle-safe, depth-capped.
3. New flags on `swarph memory links <slug>`: `--backlinks`, `--depth N`,
   `--direction in|out|both`. Default (no new flags) = today's behaviour exactly.
4. `--json` emits the parent spec's OKF **edge** records.
5. Extend the `swarph_memory_navigate` MCP tool with `backlinks`/`traverse` ops
   (whitelisted, fail-safe → `[]`).
6. README §memory update + version bump `0.31.0 → 0.32.0`.

**Out of scope:** any gbrain change; wiring to `traverse_graph`/`get_backlinks`;
the gbrain edge backfill; the Phase-2 `swarph brain` unified walker (sub-project C).

## Architecture

```
swarph memory links <slug> [--backlinks] [--depth N] [--direction in|out|both] [--json]
        │
        ▼
  memory.traverse(url, token, slug, depth, direction)      ← file-native BFS
        │  edges via okf_links.parse_okf_links(page body)
        ├── direction=out  → BFS following forward [[links]]; fetches only reachable pages
        ├── direction=in   → reverse index (one full-corpus scan) then BFS backward
        └── direction=both → union of the two
        │
        ▼
  gbrain (page store): list_pages / get_page over MCP  (memory._mcp_call, unchanged transport)
```

Nothing new touches the network transport — `memory._mcp_call` / `brain_ask`
endpoint+token resolution are reused verbatim. The only new I/O pattern is the
**corpus scan** needed for reverse (`in`/`both`) traversal.

## Components & interfaces

All additions live in `src/swarph_cli/commands/memory.py` (the file this feature
belongs to; it stays focused — one verb, one responsibility). Signatures:

- `parse_links(content)` — **retire** the local wiki-only regex; re-export
  `okf_links.parse_okf_links` under the name for any importer, and call the
  shared parser everywhere. (One grammar, pinned, tested — no second copy.)
- `_all_page_slugs(url, token) -> list[str]` — every page slug via `list_pages`
  with a high limit (single call). Used only by reverse traversal.
- `_forward_targets(url, token, slug) -> list[str]` — `parse_okf_links` of one
  page's body (`get_page(slug).content`). The out-edge primitive.
- `_reverse_index(url, token) -> dict[str, list[str]]` — full-corpus scan:
  for every page, map each forward target → list of pages linking to it. One
  unreadable/missing page is **skipped**, never aborts the scan. The in-edge
  source of truth.
- `traverse(url, token, slug, depth, direction) -> list[tuple[str, str, int]]` —
  BFS returning ordered `(from, to, hop)` edges, cycle-safe (visited set),
  `depth` clamped to `[1, DEPTH_CAP]` (`DEPTH_CAP = 10`, mirroring gbrain's own
  `TRAVERSE_DEPTH_CAP`). `direction` ∈ `{"out","in","both"}`.
- `backlinks(url, token, slug) -> list[str]` — sugar: single-hop `in` targets
  (the pages linking to `slug`), order-preserving.
- `links(url, token, slug) -> list[str]` — unchanged public behaviour
  (single-hop forward), now via the shared parser.
- `_as_okf_edge(frm, to, direction, hop) -> dict` — the OKF edge record (below).

### CLI flag semantics (`swarph memory links`)

- `--depth N` (int, default `1`) — hops. Clamped to `[1, 10]`.
- `--direction {out,in,both}` (default `out`).
- `--backlinks` (flag) — sugar for `--direction in`. **Rejected** (exit 2, clear
  message) if combined with an explicit `--direction`, to avoid an ambiguous
  request.
- No new flags → `depth=1, direction=out` → **byte-for-byte the current output**.
- Human output: discovered target slugs, order-preserving de-dupe, one per line
  (unchanged shape). For `both`, out-edges then in-edges.
- `--json` → a JSON array of OKF edge records.

### OKF edge record (`--json`)

Mirrors the parent spec's node/edge schema and the timeline sub-project's
`_as_okf` style (hemisphere-tagged, direction-carrying — the exact keys the
Phase-2 walker will read):

```json
{"type": "edge", "hemisphere": "knowledge", "from": "<slug>",
 "to": "<target-slug>", "rel": "links", "direction": "out", "hop": 1}
```

`direction` is `"out"` or `"in"` per edge (a `both` traversal yields a mix).
This changes the `links --json` shape from the current flat slug-array to edge
records — an intentional, documented enrichment for a young (0.30-era) command
with no external consumers; noted in the README changelog line.

## Reverse-scan cost (no silent caps)

`--backlinks` / `--direction in|both` perform **one full-corpus scan** —
`list_pages` + `get_page` per page (≈198 calls today). This is O(N) per
invocation and correct; it is *not* cached across invocations. The README §memory
note states this plainly and points at the (separate) gbrain edge-index backfill
as the eventual O(1) fast-path. `out`-only traversal never scans the corpus — it
fetches only pages reachable from `slug` within `depth`.

## Error handling (fail-safe contract, unchanged)

- CLI: every new path stays inside the existing `try/except` → stderr note +
  non-zero exit, **never a traceback** (matches `get`/`list`/`links`).
- Corpus scan: a single page that 404s or fails to parse is skipped; the scan
  continues and returns a partial graph rather than failing the command.
- MCP: `_memory_navigate` swallows everything to `[]` (host never sees a raise).
  The new `backlinks`/`traverse` ops are **explicitly whitelisted**; an
  unrecognised `op` returns `[]` (same guard the timeline navigate op added).

## Testing (TDD)

Unit, stdlib `unittest` + the repo's pytest runner (`venv/bin/python -m pytest`),
gbrain stubbed via a fake `_mcp_call` (no network). Cases:
- shared-parser migration: `[[slug|alias]]`→`slug`, `[[slug#h]]`→`slug`,
  `![[embed]]`, `[text](path.md)` all resolve (proves the upgrade over the old
  wiki-only regex).
- `traverse` out depth-1 == `links` (back-compat).
- out depth-2 BFS follows the frontier; depth clamp at 10; **cycle** (A↔B) terminates.
- reverse index: backlinks of a target = every page whose body links it; a page
  that fails to fetch is skipped, scan still returns.
- `both` = union, out-then-in ordering.
- `--backlinks` + `--direction` → exit 2.
- `--json` emits well-formed OKF edge records with correct `direction`/`hop`.
- MCP `backlinks`/`traverse` ops return records; unknown op → `[]`; backend
  error → `[]`.
- version pin flips to `0.32.0`.

## Global constraints

- stdlib-only; public PyPI package; no new dependencies.
- Additive verb-flag change; **no hook touch**; inert-safe.
- Default `links` output unchanged (back-compat).
- Publish to PyPI is **commander-gated**; the plan ends at green + PR. A new
  public surface → PR left for review (not auto-merged under standing green-auth).
- Stage only named files; never `git add -A`; never stage `.codegraph/` or stray docs.

## Decoupled follow-up (separate card — NOT this spec)

**gbrain edge backfill.** lab's brain is 198 pages / **0 edges**. Run gbrain's
designed link reconciliation (managed sources `markdown`/`wikilink-resolved`) to
populate the edge table from page bodies, so `traverse_graph`/`get_backlinks`
light up for *all* consumers (brain-ask, the walker). This mutates prod brain
data (write scope) → **commander-gated**; exact trigger (re-ingest vs sync vs
`run_doctor`) to be determined. Tracked separately so this shippable CLI feature
never blocks on a gated brain migration.
