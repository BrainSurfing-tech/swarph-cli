---
name: lej-session-board
description: Run one Claude Code CLI session as the manager for the fleet's other sessions, and keep the LEJ Session Board artifact current. The board gives each session a section with its state, and each cell's open swarph rows that wait on the commander, with a recommended option, an editable reply and a copy button. Use when the user asks which CLI sessions are stale, in progress or need follow-up, asks for a session board, or says "session board" or "manager session".
---

# LEJ Session Board (CLI)

One Claude Code CLI session is the **manager**. The board is one HTML page, published with `Artifact`, rendered from the JSON in `assets/board-template.html`. The markup stays as shipped. Each update replaces the JSON in `<script id="board-data">` and republishes the same file path.

This skill does not use Claude Desktop session tools. The CLI tools are `ListAgents`, `SendMessage`, the local transcript files, and `Artifact`.

A test run or a CI run must not call `Artifact` with a live roster or live board rows. Publishing real mesh data to a public artifact is a failure. The manager publishes only in the commander's own session.

## 1. Take the roster

1. Run `ListAgents`. The first line names this session (`This session is <name> [<id>]`). That name is the manager. The rows under `Peer sessions` are everyone else.
2. Sort each row:
   - **Stale:** status `offline`. Offline Remote Control rows are leftovers. Report them. Do not archive them. The commander archives those in the app. There is no CLI archive.
   - **Needs follow-up:** the cell has an open swarph row that waits on the commander (the card text matches the intendant's commander gate: commander-gated, held for the commander, or waiting on the commander).
   - **In progress:** status `busy`, `shell`, or `idle` with no commander-waiting row. `shell` is in progress. It still has a next step of its own.
3. Map a ListAgents name to its transcript through `~/.claude/sessions/<pid>.json`. That file carries `name`, `sessionId`, and `cwd`. The transcript is `~/.claude/projects/<cwd-as-dir>/<sessionId>.jsonl`. The bracketed id on the ListAgents row is not a transcript id. Read the last few `user` and `assistant` turns of that file. A Remote Control or cloud row has no local sessions file. Ask that session for a status update with `SendMessage`, `to` set to the name `ListAgents` printed.

## 2. Questions come from the swarph board

Each cell section lists that cell's open rows that wait on the commander. A question has two or three options and exactly one `"rec": true`. The recommended option is the reply you would send. Copy puts the session name, then `Re: <question>`, then the reply.

Build the JSON with `scripts/build_board.py`. It reads a `ListAgents` listing and a JSON file of those rows. It does not send anything.

## 3. Publish one DM

`scripts/build_board.py` builds the JSON. The manager sends the commander **one** DM, `kind=status`, whose content is the envelope from `envelope()`:

```
SWARPH-BOARD v1
{ ...board JSON... }
```

Send it only when that JSON changed. `publish_if_changed` compares the previous envelope and calls the sender only on a change. The `updated` clock is not a change. An unchanged board sends nothing.

Do not call `Artifact` with a live roster or live board rows. A test run or a CI run must not send this DM to the mesh. Publishing real mesh data is a failure.

## 4. Answers

An answer DM whose body has a line `Re: <title>` (cc the manager) drops that question. `drop_answered` removes the matching row before the next board. A relayed yes approves building, not deploying. A question whose accept names a deploy or a hard gate has `in_session: true`: the go is typed in that session, and the app does not send it.

## Files

- `assets/board-template.html`: the page. Do not change its markup.
- `references/data-schema.md`: the JSON fields.
- `references/coordination-rule.md`: the `CLAUDE.md` block. It addresses sessions by the name `ListAgents` prints.
- `scripts/build_board.py`: turns a listing and commander-waiting rows into board JSON.
- `LICENSE`: MIT, Copyright (c) 2026 Jonathan Edwards. Keep it with the skill.
