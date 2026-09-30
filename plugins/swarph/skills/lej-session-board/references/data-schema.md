# Board data schema

The board page renders from the JSON in `<script type="application/json" id="board-data">`. Edit this block and republish; the markup doesn't change.

```json
{
  "updated": "Monday, Oct 5, 2026, 4:30 pm ET",
  "projectFolder": "~/code/my-project",
  "manager": "Project Manager",
  "todoIntro": "One or two sentences above the to-do list.",
  "sessions": [ Session, ... ],
  "groups": [ Group, ... ]
}
```

## Session

| Field | Meaning |
|---|---|
| `id` | Short stable id, such as `prj2`. |
| `name` | The session's title, exactly as it appears in the sidebar. |
| `state` | One line: what it's doing, or what it needs. |
| `tone` | `"needs"` (terracotta, waiting on the user), `"done"` (green), or `""`. |
| `where` | Two to four plain lines on where it stands. Plain text, escaped on render. |
| `questions` | Zero or more `Question`s. |
| `pastes` | Optional. Ready-made text the user pastes somewhere else, such as a prompt for another machine: `{ "id", "title", "to", "note"?, "text" }`. It shows as an editable block, and Copy puts the text alone, with no header. |

## Question

| Field | Meaning |
|---|---|
| `id` | Unique and stable. The page keys the user's pick, edits and copied state on it. Change it when the question changes. |
| `to` | Where the reply gets pasted, such as `"PRJ2"` or `"PRJ2 (type the go there yourself)"`. |
| `title` | The question. Copy puts the session's `name`, then `Re: <title>`, then the reply. |
| `note` | Optional context. Keep it under three lines. |
| `options` | Two or three `{ "label", "text", "rec" }`. `label` is what the user reads, `text` is the full reply the session gets, and `rec: true` marks the recommended option, which is selected by default. |
| `to_node` | The cell that receives the decision. The app sends the answer DM to this name. |
| `card` | Optional. The swarph card id this question refers to. |
| `obligation` | Optional. The swarph obligation id this question refers to. |
| `in_session` | `true` when the choice must be made inside that session (a deploy or a hard gate named in the row's accept). The app shows it as answer-in-session and does not send. |

## Group

| Field | Meaning |
|---|---|
| `title` | For example "Your call", "Other people", "Claude builds", "Later". |
| `items` | `{ "text", "starter"? }`. `text` may hold `<b>` and `<code>` and is trusted HTML, so write it yourself. |

`starter` is `{ "title": "PRJ5 Short name", "prompt": "<context>…</context>\n\n<instructions>…</instructions>\n\n<output>…</output>" }`. The page shows the title above an editable prompt. Copy puts the title, a blank line, then the prompt.

## What the viewer's browser keeps

The option picks, edited replies, edited prompts and "✓ Copied" marks stay in the viewer's browser (localStorage, keyed by id), so a republish doesn't wipe them. Give a question a new `id` when its wording changes, so an old edit doesn't stick to it.
