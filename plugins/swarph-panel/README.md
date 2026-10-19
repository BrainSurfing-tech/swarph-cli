# swarph-panel

Card #1224. A Claude Code UI module that shows the **oldest open commander row** above the prompt: its
title, one button per tap option, and an "N of M open" counter. One row at a time, so asking never blocks
the cell the way `AskUserQuestion` does.

Status: **build only.** It is not listed in `.claude-plugin/marketplace.json` and nothing installs it.
Installing a new plugin module into the CLI is the commander's call. The tap buttons are a **stub** (see
"Tap path").

## How it works

```
session.start / turn.complete --> hooks/panel/refresh.py  (python3, stdlib only, not in the render path)
        swarph board obligations list --status open --json --as lab-ovh
        swarph board cards thread <card> --json --as lab-ovh     (one per untagged commander-held card)
        --> ~/.claude/.swarph-panel-rows.json   ($CLAUDE_CONFIG_DIR/ when set)

ui.render AbovePrompt --> hooks/panel/register.tsx  reads that one file, nothing else
```

- **Cache path:** `~/.claude/.swarph-panel-rows.json` (or `$CLAUDE_CONFIG_DIR/.swarph-panel-rows.json`),
  the same directory ponytail keeps its flag files in. Shape: `{updated_at, board_url, rows: [{id, card_id,
  created_at, title, options: [{label, text, outcome}] | null}]}`.
- **Render:** sorts rows by `created_at`, draws the first, returns `next(e)` at 0 rows (a missing or
  broken cache counts as 0 rows), and draws the rest of the slot below itself when another module is there.
  No network, no program, no CLI call. Control characters in a title are stripped before they reach the terminal.
- **Options:** `options` is `null` when the title does not parse, or when a tap would close nothing (step
  `validate` / `plan-review`, or a `[commander]` row held by another cell), or when the title is empty. The
  panel then shows the title and an "open board" button only. No guessed buttons.
- **"open board"** runs `xdg-open` on `board_url` from the cache, only if it is an `https://` address
  (`SWARPH_PANEL_BOARD_URL` or `--board-url` when running `refresh.py`). With none set it toasts where the
  row waits.
- **Refresh:** `refresh.py` skips if the cache is under 60 s old (`--min-age`), keeps the last good cache when the
  CLI fails (a failed read is never written as "0 rows"), and writes through a temp file and a rename.
  It runs one read-only CLI as `--as lab-ovh` (override with `--as`) and reads no token file: the CLI
  resolves its own credential. A subagent's turn does not trigger it.
- **Port:** `_tap_options`, `_tap_closable`, `commander_title`, `offer_question`, `untagged_title` are copied
  verbatim from `~/tools/board-publisher` (outside this repo, not imported). `tests/tap_golden.json` pins
  them to what the original answers. The same row rules as `publish_board.commander_rows` apply: keep a
  row tagged `[commander]`, or held by the commander; title from the tag, else the card-thread offer
  question, else the accept text.

## Tap path (what a board tap does today, and why the press is a stub)

Read from `~/mesh-gateway/server.py` and its tests (line numbers are for the file as read on 2026-10-19; they drift):

1. The commander's board app, logged in with the commander's **Meta-Edge SSO session** (`user_identity`,
   regime `meta_edge`, node `commander`), calls `POST /messages` (`server.py:5424`) with `from_node:
   commander`, the answer text, `reply_to` = lab-ovh's bound ask. The gateway records how the DM was
   authenticated (`server.py:5706-5712`). That is the DM id in the evidence (#75403 for row #2272).
2. The same session closes the row: `POST /board/obligations/{id}/close` (`server.py:16575`) with evidence
   `Board answer sent (DM #<id>): <text> ask:<ask id> relayed-from=msg:<id>`. The commander branch is
   `server.py:16691` (`_sso_is_commander`, `server.py:15934`); the direct ask citation is checked at
   `server.py:13843-13865` and `15988`. Row #2272 reads `closed_by: commander` with the SSO login recorded.
   The string "Board answer sent" appears in no file of `mesh-gateway/server.py`, `tools/board-publisher` or
   this repo; the code that produces it was not located (the app is the likely author, unconfirmed).
3. The alternative is the relay: lab-ovh's own per-peer token closes with `relayed-from=msg:<id>`, and the
   gateway checks the cited DM was recorded as the commander's SSO session (`server.py:16025-16117`, never
   `from_node`). That path accepts the bare word yes/no only (`server.py:16102-16117`), so it cannot carry a
   choice such as "Lab".

**The panel does not wire the press.** Step 1 and step 2 both need the commander's SSO bearer. A plugin
module has no such credential, by design, and must not borrow lab-ovh's token to speak as the commander.
With `COMMANDER_SSO_ENFORCE=1` (`server.py:15922`; off by default in the code, and the live value is set in
an env file that was not read) the gateway refuses every static credential on a commander row except the
verified relay (`server.py:16686-16750`).
`register.tsx` shows a toast that says the tap was **not** recorded. To wire it, the commander's own session
would have to present its SSO token to the two endpoints above (a decision for the commander, not a
default), then the panel must read the row back from the gateway and let the next refresh advance it.

## Tests

```
HOME=$(mktemp -d) CLAUDE_CONFIG_DIR=$HOME/.claude claude plugin test plugins/swarph-panel   # the *.test.tsx files
python3 -I -B -m unittest discover -s plugins/swarph-panel/tests                            # the python files
claude plugin validate plugins/swarph-panel
```

`claude plugin test` is the harness ponytail's `tests/band.test.tsx` runs under (ponytail's own `npm test`
does not run `.tsx`). It needs no network. The repo's root `pytest` only collects `tests/`, so run the
python tests with the command above.

## Not verified (needs a live TUI)

- Whether a module can redraw on a timer, or only on events (so a stale cache shows until the next event).
- What `$.command.run` does during a running turn. This module never calls it.
- That `CLAUDE_PLUGIN_ROOT` is set for `$.env.get` in a module, and that `import.meta.url` is the real
  file in the live loader. Both locate `refresh.py`; the harness covers `import.meta.url` only.
- That `xdg-open` and `<Text>` default wrapping behave in the pane.
- That a fire-and-forget `void $.process.run(...)` survives the handler returning in the live engine
  (ponytail awaits its `process.run` calls and only fires-and-forgets `$.http.fetch`).
- The event field that carries the toast text: the test mock reads `e.message ?? e.text ?? JSON.stringify(e)`.
- `import.meta.url` and `$.env.get('CLAUDE_PLUGIN_ROOT')` are not used by ponytail (see above).
- Where the panel shows (the pane it runs in, not the phone).

## Membrane map

What ponytail 5.3.2 ships for each host, from its own files. Paths are relative to
`~/.claude/plugins/cache/ponytail/ponytail/5.3.2/`. A cell says what ponytail's files show for that host,
not everything the host could offer.

| Host manifest | Offers | Evidence |
|---|---|---|
| `hooks/claude-band.json` | **UI slot with buttons** | `claude-band.json:3-5` registers a `modules` entry; `hooks/band/register.tsx:324` `on('ui.render', {component: 'AbovePrompt'})`; buttons at `register.tsx:373` and `:388-391` |
| `hooks/cursor-hooks.json` | **hooks only** | `cursor-hooks.json:4` `sessionStart` and `:10` `beforeSubmitPrompt`, command entries only; `tests/cursor-hooks.test.js:133` "Cursor has no Claude statusline" |
| `hooks/copilot-hooks.json` | **hooks only** | `copilot-hooks.json:4` `sessionStart` and `:12` `userPromptSubmitted`, `type: command` (`:6`, `:14`) |
| `hooks/claude-codex-hooks.json` | **hooks only** (no status line in the manifest) | `claude-codex-hooks.json:3` `SessionStart`, `:16` `SubagentStart`, `:28` `UserPromptSubmit`, all `type: command`. The Claude status line is a user `statusLine` setting that `hooks/ponytail-activate.js:72-89` only nudges to set up; no manifest names `ponytail-statusline.sh` |
| `hooks/qoder-hooks.json` | **hooks only** | `qoder-hooks.json:4` `UserPromptSubmit` and `:14` `PreToolUse`, command entries only |
| `gemini-extension.json` | **none of the three: context file only** | `gemini-extension.json:5` `contextFileName`; the file (lines 1-6) has no `hooks` key |
| `opencode.json` | **hooks only** (the file itself is a bare schema stub) | `opencode.json:2` `$schema` is the whole file. The real entry is `package.json:19` `main` -> `.opencode/plugins/ponytail.mjs`, which registers session hooks (`ponytail.mjs:162` `context`, `:163` `compaction`) that add a system-prompt part (`:160`) and transforms commands (`:131`): no UI call |
| `pi-extension/` | **status line only** (plus notifications; no buttons) | `pi-extension/index.js:98` `ui.setStatus("ponytail", ...)` (guarded at `:86`); `:108`, `:134`, `:146` `ui.notify`; every other `ui.` use is `ui.theme` (`:89`); `:127` `registerCommand` |
