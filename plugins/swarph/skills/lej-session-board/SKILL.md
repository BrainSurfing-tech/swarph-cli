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
   - **In progress:** `busy`, or `idle` with no commander-waiting row. It still has a next step of its own.
3. Judge a local session from its transcript tail, not from a restarted timestamp. The tail is `~/.claude/projects/<dir>/<session-id>.jsonl`. Read the last few `user` and `assistant` turns. A Remote Control or cloud row has no local tail. Ask that session for a status update with `SendMessage`, `to` set to the name `ListAgents` printed.

## 2. Questions come from the swarph board

Each cell section lists that cell's open rows that wait on the commander. A question has two or three options and exactly one `"rec": true`. The recommended option is the reply you would send. Copy puts the session name, then `Re: <question>`, then the reply.

Build the JSON with `scripts/build_board.py`. It reads a `ListAgents` listing and a JSON file of those rows. It does not send anything.

## 3. Publish

Fill `board-data` in a copy of `assets/board-template.html`. Publish with `Artifact` (`title`: "LEJ Session Board", `icon`: "list") from the manager session only. Republish the same path so the URL stays.

## 4. Relay

The user pastes blocks that start with a session name and a `Re:` line. `SendMessage` each block to that name, and add "relayed from the manager session". A relayed yes approves building, not deploying. A production deploy needs the user's "go" typed in the session that deploys.

## Files

- `assets/board-template.html`: the page. Do not change its markup.
- `references/data-schema.md`: the JSON fields.
- `references/coordination-rule.md`: the `CLAUDE.md` block. It addresses sessions by the name `ListAgents` prints.
- `scripts/build_board.py`: turns a listing and commander-waiting rows into board JSON.
- `LICENSE`: MIT, Copyright (c) 2026 Jonathan Edwards. Keep it with the skill.
