# Single-cell memory consolidation (card #1570, BUILD 1)

`swarph dreaming run` remains the legacy read-only verifier with its existing
exit contract. New verbs never call the old organizer's tail-trimming code.
Nothing here installs a unit, runs a pilot, or applies memory automatically.

## Propose, review, apply, recover

```text
swarph dreaming propose --cell CELL --corpus MEMORY --harness claude --model MODEL --questions QUESTIONS.json --out EMPTY_PRIVATE_DIR
swarph dreaming apply --cell CELL --corpus MEMORY --proposal EMPTY_PRIVATE_DIR
swarph dreaming restore --cell CELL --corpus MEMORY --backup BACKUP.tar.gz
```

`claude` and `codex` reuse the bench transports exactly, including their isolated
argv, channel/billing environment scrub and billing guards. Codex requires
ChatGPT auth; Claude refuses a non-subscription result. Muse/opencode and all
other harnesses are refused **before spawning**. No real harness call was used
to validate this build; fake executables record the actual argv/environment.
The transport's model-generated text is data, never a command or file-tool call.

The JSON edit list uses exactly these shapes:

```json
[
  {"op":"rewrite","files":["lesson.md"],"target":"lesson.md","text":"full replacement text"},
  {"op":"merge","files":["first.md","duplicate.md"],"target":"first.md","text":"full merged text"},
  {"op":"retire","files":["old.md"]},
  {"op":"promote","files":["MEMORY.md","important.md"],"target":"MEMORY.md","text":"full new index"}
]
```

`demote` has the same shape as promote. Each source/target must be a manifest
basename matching `[\w.-]+\.md`; unknown/traversing names, extra fields and
overlapping source lists are refused. A target must be one of its sources.
The index must be explicitly named to edit it. Review `edits.json` and the
private `changes.diff` before apply; model claims are not verified facts.

The manifest pins the original live hashes. Apply rechecks every named file,
archives every changed pre-image, then rechecks before each file's temp+rename
or archived removal. Changed/missing/symlinked sources are skipped and reported.
The directory is **never** replaced; unlisted files, nested dirs and non-md
entries stay untouched. Retire and non-target merge sources are copied durably
to the archive before removal. Archives are outside the corpus glob and are
never pruned. This preserves exact old text even if a rewrite loses a fact.

`user_*`, `feedback_*`, and frontmatter `type: user` / `type: feedback` are
propose-only unless the owner deliberately supplies `--allow-owner-notes`.
Default apply/restore idle window: 15 min (`--idle-minutes`); every corpus has
one crash-released nonblocking lock, shared even across different cell names.
Live writers do not take this lock: the idle/SHA checks detect their changes,
but this is not a kernel compare-and-swap against a non-cooperating writer.

Before the first live write a whole-directory tar.gz (including non-md/nested
entries and symlinks as links, never followed) is fsynced, read back and checked
for the same entry count and content hashes. Default location:
`~/.local/state/swarph/dreaming/CELL/CORPUS_HASH/backups/`, **not /tmp**.
Backups are 0600, state/run/archive directories 0700. Last 5 backups retained
(`--keep-backups`); `--state-root` lets an operator choose another persistent
disk. Tests override it to scratch; do not choose temporary storage in use.
Restore validates provenance/paths/hashes and makes a new recovery backup
first, then restores entries file-at-a-time. New unrelated files are preserved,
not deleted. POSIX permission/locking behavior is tested; native Windows harness
execution/ACL behavior is not established by the POSIX fake-binary fixtures.

## Incremental budget and private reports

Cursor: `STATE_ROOT/CELL/CORPUS_HASH/cursor.json`, **never shared by HOME**.
Changed files are ordered by filename family and duplicate hash, batched, and
only successful included batches advance. A capped retry does not reprocess
already covered unchanged files. Oversized notes remain due; increase the
budget or split a note deliberately rather than silently truncate it.

`--token-cap=32000` is a **soft estimated per-run budget**, input estimate
`ceil(UTF-8 bytes/4)` including the purpose prompt. Each batch is checked before
spawn; reported input+output
usage also debits the remaining budget. The last generated response and CLI
fixed instruction floor cannot be pre-capped by the bench transports. This is
not a hard billing ceiling. No single-call mode is implied.

`edits.json`, full clone, and `changes.diff` stay in the private run directory.
With no `--out`, a unique run is created under per-cell state `proposals/`.
`report.json` and optional `--notify-card 1570` carry **only counts, basenames
and operation types**, never note text, diff/changelog prose or error bodies.
Counts include file/index byte/feedback file totals and unresolved index links
before/after. An unresolved-link count is a finding for review, not an automatic
repair or a claim the proposed memories are correct. `--gateway`/`--token-file`
use the existing cell identity; a failed report refuses the run without cursor
advance. Apply/restore may already have written safely backed-up changes before
a report failure: inspect the private apply receipt before any retry.

## Frozen paired recall instrument (no live pilot)

A different cell prepares `QUESTIONS.json` **before the first propose**:
`{"tag":"pilot-v1","questions":[{"id":"q1","question":"...","answer":"exact id/path/number"}, ...]}`.
At least 30 unique questions; answers are script-matchable strings/ints.
The first propose records its SHA256/tag and timestamps in per-cell state.
No supplied set means no evaluable recall baseline; it cannot be retrofitted
after consolidation. No questions are generated from live transcripts here.

The pilot operator obtains before/after reader responses using the same model
and set. Both answer files have `model`, `question_set_tag`,
`question_set_sha256`, and `answers` (a complete mapping from id to string/int).

```text
swarph dreaming recall-score --cell CELL --corpus MEMORY --questions QUESTIONS.json --before BEFORE.json --after AFTER.json
```

The offline scorer refuses changed/unpinned/late sets, fewer than 30 questions,
different model/set tags or missing answers; outputs before/after correct counts
and their paired difference. Declared model tags are not served-model telemetry.
Synthetic instrument tests are not evidence of improved real recall.

## Operator-only nightly templates

`deploy/dreaming/swarph-dreaming@.service` and `.timer` are shipped, not installed.
Commander/operator reviews one instance per cell and creates the private
`/etc/swarph/dreaming/CELL.conf` with **paths/labels only**:
`DREAMING_CORPUS`, `DREAMING_HARNESS`, `DREAMING_MODEL`, `DREAMING_QUESTIONS`,
`DREAMING_GATEWAY`, `DREAMING_TOKEN_FILE`. No billing keys or token values.
Adjust User/HOME/executable path for that host. Timer: 01:00 UTC plus up to
30 min random delay; no daytime boot catch-up, outside weekday 14:30–21:00 UTC.
It proposes only. Pilot order remains lab-ovh then gpt-ops, both propose-only;
actual apply waits for an owner-reviewed diff. Native Mac/Windows scheduling,
transcript analyzer fan-out and other harnesses are not installed by BUILD 1.
