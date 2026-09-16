# OpenCode membrane — probe/grounding plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to run this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the five open assumptions in the spec
(`docs/superpowers/specs/2026-09-11-opencode-membrane-design.md`, §"Open questions / probes required")
into **measured facts**, so the membrane build that follows never guesses at opencode behavior. This plan
ships *facts + doc edits only* — no `src/` code, no membrane.

**Architecture:** Five probes, each settles one spec assumption by running the real opencode binary
(v1.18.30) in an isolated scratch tree. Each probe records its finding back into the spec's
"Grounded opencode facts" section (replacing the `#P*` assumption with a measured fact + the command
that produced it). Probes are non-destructive and clean up after themselves; nothing is written into
the operator's `~/.config/opencode` or `~/.local/share/opencode`.

**Tech Stack:** bash + the real `opencode` binary + `tmux` (probe #P3 only). No Python, no pytest. The
committed artifacts are the edited spec and this plan.

**Spec:** `docs/superpowers/specs/2026-09-11-opencode-membrane-design.md` (author: commander, 2026-09-11).
Branch `feat/opencode-membrane` off `main` in `/home/ubuntu/swarph-cli` (PUBLIC repo).

## Global Constraints

Every probe inherits these. Each is a doctrine already paid for on a live box elsewhere in this repo.

- **Ground by execution, never assume.** The cursor membrane (`test_cursor_membrane.py`) shipped only
  after two probes overturned an analogy; the vibe membrane (`_scrub_vibe_namespace`) documents a
  whole comment correcting a wrong mechanism. An unrun probe is an assumption; an assumption shipped
  becomes a defect one card later.
- **Distinguish MEASURED from CLAIMED.** Read-only inspection (SDK types, docs) is CLAIMED; running
  the binary and observing output is MEASURED. Every recorded fact carries which of the two it is.
- **Loaded ≠ fired.** The multi-membrane spec's own table names the family: cursor-agent/claude
  HONOUR, codex/grok-cli GATE (hook installed, never fires). A probe must observe the hook **firing**,
  not merely the plugin file loading — an armed-looking-but-deaf plugin is the exact failure class.
- **Non-destructive + self-cleaning.** All state lives under `/tmp/opencode-probes/<pid or ts>/`; the
  operator's real config/data dirs are never read for mutation and only read where the finding is
  *about* them (e.g. `db path`). Every probe's last step removes its scratch tree. Set
  `OPENCODE_DISABLE_AUTOUPDATE=1` on every invocation so a probe never triggers a self-update.
- **A finding names its version and its command.** Every recorded fact is tagged `opencode 1.18.30` +
  the exact command line, so a future version change is detectable ("config claim is not a runtime
  fact" — science-claude, #452).
- **No model call where the question is not about the model.** `session list`, `db path`, plugin-load
  and `--continue`-on-empty are settled without a paid turn where possible; only the firing proofs
  (#P4/#P5) and the TUI capture (#P3) need a real turn, and those use the smallest prompt available.
- **PUBLIC repo → topology-free commit bodies.** Probe findings mention no host/box names.

## File Structure

| File | Responsibility |
|---|---|
| `docs/superpowers/specs/2026-09-11-opencode-membrane-design.md` | MODIFY: replace each `#P*` assumption with its measured fact under a "Probe findings (2026-09-11)" block. |
| `docs/superpowers/plans/2026-09-11-opencode-probes.md` | this plan; tick the checkboxes as probes land. |

No other files change.

---

### Task 1 (#P1): session discovery — does `session list` name the workspace?

**Settles:** `_opencode_has_prior_session()` — the guard that decides whether `--continue`/`--session`
is safe for a given cell cwd, and which resume form per-workspace resume actually uses.

**Question, precisely:** (a) Does `opencode session list --format json` include enough of a directory
field to map a session to `cell.cwd`? (b) What does a bare `--continue` do when the store has **no**
session for that workspace — fresh start or error?

- [ ] **Step 1: create an isolated scratch tree + a real session in it**

```bash
export OC="~/.opencode/bin/opencode"
P=/tmp/opencode-probes/p1; mkdir -p "$P/a" "$P/b"
cd "$P/a" && OPENCODE_DISABLE_AUTOUPDATE=1 "$OC" run "reply with the single word ok" >/dev/null 2>&1
```

- [ ] **Step 2: inspect the session list shape**

```bash
cd "$P/a" && "$OC" session list --format json | head -c 2000
```

Record: is there a per-session file/directory key (e.g. `directory`/`worktree`/`project`)? This is the
field that makes per-workspace resume possible at all. Also run `"$OC" session list --format json --help`
to confirm the flag set on this exact version.

- [ ] **Step 3: the empty-store continuity probe**

```bash
cd "$P/b" && OPENCODE_DISABLE_AUTOUPDATE=1 "$OC" run --continue "reply ok" >/tmp/opencode-probes/p1-empty.out 2>&1; echo "rc=$?"
```

Record rc and the first lines of `p1-empty.out`. Three outcomes, three different membranes shapes:
fresh-start-with-0 (agy's shape → unconditional `--continue` is safe), error (cursor's shape → must
guard), or prompt-to-pick (muse's shape → hung pane, worst case).

- [ ] **Step 4: record the fact + clean up**

Write to the spec under "Probe findings": the `session list` schema (which field, if any, names the
dir), the empty-store rc/message, and — the load-bearing conclusion — `_opencode_has_prior_session()`
will use `<mechanism>` and `build_argv` will emit `<resume-form>`. Then `rm -rf /tmp/opencode-probes/p1`.

---

### Task 2 (#P2): data isolation — where does opencode put `db`/`auth`/state under override?

**Settles:** `_opencode_env(cell)` — whether the cell can isolate its session DB + credentials via an
honored env (preferred: `XDG_DATA_HOME`/`XDG_STATE_HOME` or an `OPENCODE_*` knob) or whether we must
relocate `$HOME` (grok's shape, and its cost: losing `Path.home()`-relative mesh identity).

- [ ] **Step 1: baseline — confirm where the default lives**

```bash
~/.opencode/bin/opencode db path          # expect: /home/ubuntu/.local/share/opencode/opencode.db
ls -la ~/.local/share/opencode/           # db + auth.json + log + repos
ls -la ~/.local/state/opencode/           # locks + model.json + prompt-history
```

- [ ] **Step 2: the overrides-during-a-run probe (the decisive one)**

Run a real (tiny) turn under each candidate override, with a fresh data root, and see where on-disk
state actually lands. Use a timestamp marker so we attribute fresh files to the probe:

```bash
M=$(date +%s); P=/tmp/opencode-probes/p2; mkdir -p "$P/data" "$P/state"
cd "$P" && XDG_DATA_HOME="$P/data" XDG_STATE_HOME="$P/state" OPENCODE_DISABLE_AUTOUPDATE=1 \
  ~/.opencode/bin/opencode run "reply ok" >/dev/null 2>&1
find "$P/data" "$P/state" -newermt "@$M" 2>/dev/null | head -40        # what actually got written here?
ls -la ~/.local/share/opencode/ 2>/dev/null | grep -v '^total'           # did the real db move?
```

Interpretation, stated in the spec, not guessed: only if the probe's `db-wal`/`auth`/`locks` appear
under `$P/data`/`$P/state` **and** the real `~/.local/share/opencode` mtime did not advance, is the
override honored. If `auth.json` writes to the operator dir regardless, the credential **cannot** be
isolated that way (a finding, not a bug — it changes the membrane to a `$HOME`-relocation or
credential-symlink shape, like grok/vibe).

- [ ] **Step 3: check for a native `OPENCODE_*` data knob**

```bash
~/.opencode/bin/opencode --help 2>&1 | grep -iE "dir|data|state|config"
env | grep -i OPENCODE
```

Record whether any `OPENCODE_*_DIR`-style variable exists beyond the documented `OPENCODE_CONFIG` /
`OPENCODE_CONFIG_DIR` / `OPENCODE_CONFIG_CONTENT`. Absence is itself the finding (the env list in the
CLI docs shows no data-dir override — confirm by execution, not by the doc).

- [ ] **Step 4: record the fact + clean up**

Write to the spec: which isolation mechanism is honored (name it, with the probe command), and the
membrane implication — `_opencode_env` sets `<KNOB>`, and either keeps `HOME` intact or relocates it
with the mesh-identity caveat named. Then `rm -rf /tmp/opencode-probes/p2`.

---

### Task 3 (#P4 + #P5 together): the plugin — does it auto-load, does it fire, is `system.transform` live?

**Settles:** (a) whether a file in the plugin dir needs no `opencode.json` edit (installer shape),
(b) which hook events actually fire on this version (the hook table in the spec is CLAIMED until here),
(c) whether `experimental.chat.system.transform` and `experimental.session.compacting` fire, and whether
`OPENCODE_EXPERIMENTAL=1` is required.

**Why one task:** all three answers come from a single scratch plugin run under `OPENCODE_CONFIG_DIR`,
which also proves the config-dir isolation knob from #P2 in the same pass.

- [ ] **Step 1: write a scratch plugin in an isolated config dir**

```bash
P=/tmp/opencode-probes/p3; mkdir -p "$P/config/plugins" "$P/work"; cd "$P/work"
cat > "$P/config/plugins/probe.js" <<'EOF'
import { appendFileSync } from "node:fs";
export const Probe = async () => {
  const log = (k) => { try { appendFileSync(process.env.PROBE_LOG, k + "\n"); } catch {} };
  log("init");
  return {
    event: async ({ event }) => {
      if (event.type === "session.created") log("event:session.created");
      if (event.type === "session.idle") log("event:session.idle");
      if (event.type === "session.compacted") log("event:session.compacted");
    },
    "experimental.chat.system.transform": async (input, output) => {
      log("hook:system.transform");
      output.system.push("PROBE-MARKER");
    },
    "experimental.session.compacting": async (input, output) => {
      log("hook:session.compacting");
      output.context.push("PROBE-COMPACT");
    },
    "tool.execute.before": async (input) => { log("hook:tool.before:" + input.tool); },
  };
};
EOF
```

- [ ] **Step 2: run WITHOUT `OPENCODE_EXPERIMENTAL`, then WITH**

```bash
export PROBE_LOG="$P/run1.log"
OPENCODE_CONFIG_DIR="$P/config" OPENCODE_DISABLE_AUTOUPDATE=1 \
  ~/.opencode/bin/opencode run "reply ok" >/dev/null 2>&1
unset OPENCODE_EXPERIMENTAL; cat "$PROBE_LOG" 2>/dev/null

PROBE_LOG="$P/run2.log" OPENCODE_EXPERIMENTAL=1 \
  OPENCODE_CONFIG_DIR="$P/config" OPENCODE_DISABLE_AUTOUPDATE=1 \
  ~/.opencode/bin/opencode run "reply ok" >/dev/null 2>&1
cat "$PROBE_LOG" 2>/dev/null
```

Record both logs. The answers are literal: `init` present = auto-loaded with **no** `opencode.json`
`plugin` entry (installer ships the file only). `hook:system.transform` present = starter/wake injection
route is live; absent = the spec's fallback (`chat.message`) becomes the primary. `event:*` lines tell
us whether `session.created`/`session.idle` are the wake/postcompact triggers or whether we rely on the
typed hooks. A `run1.log` empty of `*transform*` but `run2.log` non-empty = `OPENCODE_EXPERIMENTAL` is
required for the experimental hooks (a real finding: the membrane must set it).

- [ ] **Step 3: record the facts + clean up**

Write to the spec: auto-load confirmed/refuted, the measured fire-map (which of
`system.transform`/`session.compacting`/`tool.execute.before`/`session.*` events fired, under which
env), and whether `OPENCODE_EXPERIMENTAL=1` is a membrane env requirement. Then
`rm -rf /tmp/opencode-probes/p3`.

---

### Task 4 (#P3): TUI idle markers — capture the real busy/composer/placeholder text

**Settles:** `PANE_PREDICATES["opencode"]` (`busy_markers`, `composer_prefixes`, `empty_placeholders`).
Empty is an acceptable **measured** result; defaulting to another TUI's markers is not.

- [ ] **Step 1: run opencode in a named tmux pane**

```bash
P=/tmp/opencode-probes/p4; mkdir -p "$P"; cd "$P"
tmux new-session -d -s oc-probe -c "$P"
tmux send-keys -t oc-probe 'OPENCODE_DISABLE_AUTOUPDATE=1 ~/.opencode/bin/opencode' Enter
sleep 6
tmux capture-pane -t oc-probe -p > "$P/idle.txt"
```

- [ ] **Step 2: capture the busy state + composer prompt**

```bash
tmux send-keys -t oc-probe 'reply with the single word ok' Enter
sleep 4; tmux capture-pane -t oc-probe -p > "$P/busy.txt"
sleep 12; tmux capture-pane -t oc-probe -p > "$P/composer.txt"
```

Record the exact idle banner (the "empty placeholder"), the busy indicator line (the marker that
appears while a turn runs), and the composer prompt character (the prefix on the input line). These
become the tuples. If the busy marker is indistinguishable from idle (no spinner/text), record that as
the measured result and leave the tuples empty — fail-closed (unknown ⇒ busy ⇒ defer).

- [ ] **Step 3: record + clean up**

```bash
tmux kill-session -t oc-probe 2>/dev/null; rm -rf /tmp/opencode-probes/p4
```

Write the measured markers into the spec and into `pane_probe.py`'s `PANE_PREDICATES["opencode"]`
note (a comment, not code — the code change is the membrane task).

---

## Done criteria

Every `#P*` in the spec's "Open questions / probes required" section is replaced by a measured fact
under "Probe findings (2026-09-11)", each tagged `opencode 1.18.30` + its command line, each marked
MEASURED vs CLAIMED, and each closing on the membrane decision it unblocks. No scratch trees remain
under `/tmp/opencode-probes/` (verify: `ls /tmp/opencode-probes/ 2>/dev/null` empty). No `src/` code
changed. Committed on `feat/opencode-membrane` with a topology-free body.

## Open risk, stated not deferred

`#P3` needs a real paid turn; `#P4/P5` need two. If the box is offline for opencode's provider at
probe time, `#P1`'s list-shape half and `#P2` are still runnable (they don't need a model), but the
firing proofs and TUI capture cannot complete. In that case record what ran, leave the remaining
`#P*` as MEASURED-BLOCKED with the reason, and treat the blocked ones as the first tasks of the
membrane build rather than silently marking them done.

## Self-Review

- Every probe command is runnable verbatim against `~/.opencode/bin/opencode` (v1.18.30); probes that
  mutate state only ever touch `/tmp/opencode-probes/*`; all set `OPENCODE_DISABLE_AUTOUPDATE=1`.
- Each task closes on "record the fact into the spec + clean up", so the plan's ONLY committed output
  is the spec edit — matches the goal (facts + docs, no membrane code).
- The three non-model probes (list shape, `db path`, empty-store `--continue` rc) are ordered before
  the paid-turn probes, so a budget ceiling still yields the cheapest high-value facts first.
