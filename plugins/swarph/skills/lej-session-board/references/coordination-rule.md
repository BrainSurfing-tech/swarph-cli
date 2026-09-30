# Coordination rule template

Put this in the project folder's `CLAUDE.md`, and fill in the manager's name. `ListAgents` prints that name on its first line. Every Claude Code CLI session that starts in the folder, or in a subfolder or worktree, loads it.

```markdown
# Session coordination

Standing rule from <user> (<date>). It applies to every Claude Code CLI session working in this folder or below it.

## Report to the <Manager title> session

One session, **<Manager title>**, is the hub. Address it by the name `ListAgents` prints, not by a desktop session id. `SendMessage` takes that name as `to`. Copy the name exactly. If two rows share it, append the row's ` [ref]` the way `ListAgents` tells you to.

Send it a short update with `SendMessage` at each of these points:
- **Start:** when you take on a task. Say what it is and which files or machines it touches.
- **Waiting on <user>:** when you need a decision, approval or go. Put the question in the update so the manager can put it on the board.
- **Before shared changes:** before a migration, a deploy, or edits to files another session may also be changing.
- **Done:** when the work ships or finishes. Give the result, the commit, and anything left open.

Keep each update to a few lines, with the result first. Also ask <user> directly in your own session, as usual; the update to the manager is extra.

When you learn that another session is working in the same area, message that session directly by the name `ListAgents` printed and agree who changes what and who deploys first. Then tell the manager what you agreed.

A production deploy needs <user>'s "go" typed in your own session. A go relayed by the manager approves building, not deploying.

The CLI has no archive. The manager reports stale sessions, including offline Remote Control rows, on the board. <user> archives those in the app.

If the name stops resolving, run `ListAgents` again and send to the session named "<Manager title>". If there isn't one, tell <user> in your own session.
```
