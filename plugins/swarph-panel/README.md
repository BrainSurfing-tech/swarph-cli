# swarph-panel

Card #1224. A Claude Code UI module that shows the **oldest open commander row** above the prompt: its
title, one button per tap option, and an "N of M open" counter. One row at a time, so asking never blocks
the cell the way `AskUserQuestion` does.

Status: **build only.** It is not listed in `.claude-plugin/marketplace.json` and nothing installs it.
Installing a new plugin module into the CLI is the commander's call. A tap button **proposes** and closes
nothing; the commander confirms on the board (see "Tap path").

## How it works

```
session.start / turn.complete --> hooks/panel/refresh.py  (python3, stdlib only, not in the render path)
        swarph board obligations list --status open --json --as lab-ovh
        swarph board cards thread <card> --json --as lab-ovh     (one per card with a commander-held row)
        --> ~/.claude/.swarph-panel-rows.json   ($CLAUDE_CONFIG_DIR/ when set)

ui.render AbovePrompt --> hooks/panel/register.tsx  reads that one file, nothing else
```

- **Cache path:** `~/.claude/.swarph-panel-rows.json` (or `$CLAUDE_CONFIG_DIR/.swarph-panel-rows.json`),
  the same directory ponytail keeps its flag files in. Shape: `{updated_at, board_url, peer, rows: [{id, card_id,
  created_at, title, options: [{label, text, outcome}] | null, ask_id, proposed}]}`. `peer` is the `--as` name
  `refresh.py` ran with; a press sends as it. `ask_id` is lab-ovh's ask for the row (its DM to the commander with
  `obligation_id` = the row id, first match, the same rule as the board page), `null` when none was found.
  `proposed` is the label of the newest `panel: <label> on #<row>` message that answers that ask, is addressed to the
  commander, comes from the cell that sent the ask, has a later id, and names exactly one of the row's option labels,
  else `null`. The board page applies the same rule before it pre-selects anything.
- **Render:** sorts rows by `created_at`, draws the first, returns `next(e)` at 0 rows (a missing or
  broken cache counts as 0 rows), and draws the rest of the slot below itself when another module is there.
  No network, no program, no CLI call. Control characters in a title are stripped before they reach the terminal.
- **Options:** `options` is `null` when the title does not parse, or when a tap would close nothing (step
  `validate` / `plan-review`, or a `[commander]` row held by another cell), or when the title is empty, or when
  the row has no ask. The module also draws no buttons without an integer `ask_id` and a `peer`. The
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

## Tap path (what a press does, and what the board does with it)

A press **closes nothing and holds no commander credential.** It posts ONE DM through this pane's own peer token:

```
swarph mesh send commander --as <peer> --kind question --reply-to <ask id> --content "panel: <label> on #<row>"
```

then toasts "proposed, confirm on the board" and runs `refresh.py --min-age 0` without waiting for it. A
non-zero exit (or a rejected run) toasts that nothing was sent instead. The ask it answers is lab-ovh's own DM to the
commander carrying `obligation_id = <row>`, found by `refresh.py` in the card thread. `--reply-to` sets `reply_to`
only; `mesh send` does not copy the parent's `thread_id` (the gateway checks that the parent exists and stores
`thread_id` exactly as sent), so this DM is not on the card's thread. A card-thread read therefore sees a `panel:`
message only if someone posted it with `--thread`; the board page reads the commander's inbox, which has it.

The commander then confirms on the board page under their own Meta-Edge SSO session, through the close path that
is unchanged (read from `~/mesh-gateway/server.py` on 2026-10-19; line numbers drift):

1. The board app calls `POST /messages` (`server.py:5424`) as the commander with the answer text and `reply_to` =
   lab-ovh's ask. The gateway records how the DM was authenticated (`server.py:5706-5712`).
2. The same session closes the row: `POST /board/obligations/{id}/close` (`server.py:16575`) with evidence citing the
   DM and `ask:<ask id>`. The commander branch is `server.py:16691` (`_sso_is_commander`, `server.py:15934`); the
   direct ask citation is checked at `server.py:13843-13865` and `15988`.
3. The alternative is the relay: lab-ovh's own per-peer token closes with `relayed-from=msg:<id>`, and the gateway
   checks the cited DM was recorded as the commander's SSO session (`server.py:16025-16117`, never `from_node`). That
   path accepts the bare word yes/no only (`server.py:16102-16117`), so it cannot carry a choice such as "Lab".

The proposal DM is a pointer for the commander, not the answer: the relay checks the cited DM against the
commander's recorded SSO session, and this one is sent under a peer token. The board page pre-selects the proposed
option and labels its button `confirm: <label> on #<row>`.

**Debounce and hotkeys (build #2437).** A press that sends locks its row for the rest of the session: further
presses on that row send nothing and toast `already proposed for #<row>`, until the panel shows a different
row. A failed send never locks. (Measured need: 4 `panel: Yes on #2436` DMs in 30 s, card #1224 thread 77416.)
Each option button also carries a digit hotkey (`1`..`9` in option order), the Button API's focus-free key for
terminals where clicks never reach the panel. Whether the AbovePrompt slot's buttons sit in the host's hotkey
band is not documented and the plugin test kit cannot inject a terminal keypress into this drawing, so that
last step needs one live-terminal check after install; the panel was not moved on a guess.
With `COMMANDER_SSO_ENFORCE=1` (`server.py:15922`) the gateway refuses every static credential on a commander row
except the verified relay (`server.py:16686-16750`); nothing here goes near that.

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
- Whether `$.process.run` rejects or resolves with an `exitCode` when the program exits non-zero. The press handles both.
- That `swarph mesh send --as <peer>` finds the peer's token on the machine the pane runs on (the CLI resolves it).
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
