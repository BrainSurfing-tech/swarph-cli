# `swarph init` — cell.yaml scaffolder (design)

**Status:** AI²-CONVERGED + SHIPPED (2026-06-02; lab spec + droplet review DM 1936).
Two design changes from the original flags-only draft:
- **Interactive install wizard is the primary UX** (commander direction): name → LLM-type
  menu → role/cwd/tmux → "use assisted memory? (y/n)" → symlink? → confirm. Flags override
  every prompt (fully-flagged = non-interactive for CI). `-y`/`--non-interactive` forces no-prompt.
- **`--register` DROPPED from v1** (droplet's flag A): registration under R1 mints a
  once-only per-peer token; init scaffolding from the OPERATOR's context capturing another
  cell's token is both a token-loss landmine (a failed write after mint burns the once-only
  raw → needs revoke+resurrection) AND adjacent to the forge boundary. Scaffold-only; the
  cell self-adopts from its OWN context per SWARPH_PEER_TOKEN_ADOPTION.md (forge-clean).
  Deferred follow-up: a careful single-process register (capture→mode-600→verify→fact-only).
- Folded: echo the RESOLVED cwd/tmux/cursor in output (#3, G); gh/HTTPS reachability note on
  SSH→HTTPS rewrite (#4); reuse canonical PEER_NAME_RE (C — already done, no redefine).

Motivation: we just hand-wrote two
cell.yamls (gpt-ops, gemini-researcher) and hit two avoidable snags — (a) wrote them
to the cwd instead of the named registry `cells_dir()` (`swarph spawn <name>` failed),
and (b) an SSH `assisted_memory` repo URL that this gh/HTTPS box can't clone. `swarph init`
turns the next cell into a one-liner that can't make those mistakes.

## Goal
Scaffold a **validated** cell.yaml at the canonical registry path so `swarph spawn <name>`
works immediately. Net-new verb; touches nothing else.

## Surface
```
swarph init <name> --provider {claude|codex|antigravity} [flags]
```
| flag | default | notes |
|---|---|---|
| `<name>` (positional) | — | kebab-case, validated against `PEER_NAME_RE` (no underscores) |
| `--provider` | required | validated against `VALID_PROVIDERS` |
| `--role` | = `<name>` | non-empty string |
| `--cwd` | `$PWD` | absolute-ified |
| `--tmux` | = `<name>` | → `extra.tmux_session` (watchdog F4) |
| `--cursor` | `/tmp/<name>-cursor.json` | → `extra.cursor_path` |
| `--sandbox` | provider default | codex: `workspace-write`; antigravity: omit (default-on); claude: n/a |
| `--gateway` | `http://lab-ovh:8788` | → `extra.mesh.gateway` |
| `--assisted-memory <repo>` | off | sets `{enabled:true, repo, interval_min:15}`; **HTTPS-normalized** (rewrites `git@github.com:` → `https://github.com/` since this box auths via gh) |
| `--starter <path>` | none | optional `starter_prompt_path` |
| `--register` | false | also `POST /peers/register` after writing (reuses onboard's path) |
| `--symlink-cwd` | false | also drop a `cwd/cell.yaml` symlink → registry file |
| `--force` | false | overwrite an existing registry file |

## Behavior
1. Validate `name` (PEER_NAME_RE), `provider` (VALID_PROVIDERS), sandbox-vs-provider.
2. Build the cell dict, run it through `swarph_shared.cell.parse_cell_dict` — **fail-loud** if it wouldn't parse (never write an invalid cell.yaml).
3. Write to `cells_dir()/<name>.yaml` (refuse if exists, unless `--force`). `cells_dir()` mkdir-p.
4. If `--symlink-cwd`: `cwd/cell.yaml` → registry file (single source, no drift).
5. If `--register`: POST /peers/register (mint-once per-peer token handled by onboard logic; ratify stays a SEPARATE manual trust-grant — init never ratifies).
6. Print the resolved spawn argv (dry-run style) + next steps.

## Out of scope (deliberate)
- **No ratification** (separate trust-grant; init only scaffolds + optionally registers).
- **No interactive wizard** in v1 (flags only; add prompts later if asked).
- No editing of existing cells (that's hand-edit or a future `swarph cell edit`).

## AI² open questions (for peers)
1. **`--register` default** — off (scaffold-only) or on? Lean: OFF (registration is a state-changing mesh action; opt-in).
2. **`--symlink-cwd` default** — off or on? Lean: OFF (registry is canonical; symlink is convenience, and a stray cwd cell.yaml can get auto-discovered by `swarph spawn` from that dir).
3. **`--cwd` default** — `$PWD` vs required? Lean: `$PWD` (ergonomic; you usually init from the cell's dir).
4. **assisted-memory HTTPS-normalize** — auto-rewrite SSH→HTTPS, or just warn? Lean: auto-rewrite + print a note (the SSH gotcha cost us a memory-less gemini spawn).
5. **`--provider` required vs inferred** — keep required (no sensible default across claude/codex/antigravity).

## Tests
- name-validation (underscore rejected), provider-validation, parse_cell_dict round-trips, refuse-existing-without-force, HTTPS-normalize of an SSH repo, `--symlink-cwd` creates the link, resolved-argv matches `swarph spawn <name> --dry-run`.
