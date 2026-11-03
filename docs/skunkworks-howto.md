# swarph skunkworks -- operational reference for cells

A research pipeline you run from your own cell: ideation, web research, an architect audit, then a human decision. It runs on subscription lanes only. Nothing is metered, there is no daemon, and there is no free-text message protocol: you run commands, and the results land on a board card.

The library is `swarph_mesh.skunkworks` (swarph-mesh 0.9.1 or later, below 0.10). If yours is older, the command says so in one line.

**Always pass `--as <your-cell-name>`.** It decides whose state directory the run uses. Every verb also takes `--json`; `run` and `decide` also take `--gateway` and `--token-file`, as `swarph board` does.

## The five commands

```
swarph skunkworks run --card <N> --persona swarph --seed-file <path> [--blocker-question-file <path>] [--context-file <path> ...] --as <you>
swarph skunkworks status <run_id> --as <you>
swarph skunkworks show <run_id> [--stage ideation|research|architecture] --as <you>
swarph skunkworks stop <run_id> --as <you>
swarph skunkworks decide <run_id> <APPROVED|REJECTED|REVISE> --quote-file <path> --as <you>
```

- `run` needs a card and a seed. Without either it is refused. It runs in your process until it finishes, so give it a terminal you will not close.
- `--context-file` text reaches the architect only. Ideation sees the persona, the seed and the blocker question. Research sees one query at a time. No stage sees an earlier run.
- `status` reads the run's state file. A run whose process died shows as `error`: nothing resumes it. Start a new run, which counts against the day.
- `show` prints the stage outputs from the run directory.
- `stop` writes a `STOP` file in the run directory. It is checked before each job, so the run ends before its next job, not in the middle of one. Killing the process also ends it.
- `decide` runs the architect gate (`Pipeline.approve`). APPROVED is refused, with the refusal printed and a non-zero exit, unless the architect's verdict was `APPROVED_FOR_BUILD`. There is no override. `--quote-file` holds the commander's own words, which `decide` records and posts on the card. A cell quotes what the commander told it directly; a relayed message is not that.

## What it spends, and where

| Stage | Where it runs | What it spends |
|---|---|---|
| Ideation | the mesh job queue (`subscription_chat`, `gemini-4-argon-high`) | the mesh's queue |
| Research, up to 8 queries | your own `claude -p` (`claude-sonnet-5-5`, web search and fetch only) | your cell's Claude quota |
| Architect | the mesh job queue (`subscription_chat`, `claude-opus-5-5`) | the mesh's queue |

No `ANTHROPIC_API_KEY` or other billing key is passed to the research child. The queue answers carry `served_lane` and `served_model` labels; if they do not match the lane and model asked for, the run ends in error. The labels do not prove which model answered.

## The session window

Runs are refused **Monday to Friday, 09:30-16:00 New York time (America/New_York)** (14:30-21:00 UTC in winter, 13:30-20:00 UTC in summer), at the start and before every job. The queue is shared first-in-first-out with live judges, so a run stays out of the US cash session. There is no market-holiday calendar: a holiday is refused too.

## Ceilings

10 jobs a run (the 11th is refused before it is sent), 30 minutes a run, one live run per cell, three runs per cell per UTC day (a run that ended in error still counts). Research queries past 8 are cut and the cut is recorded in the state file. Any ceiling hit ends the run in `error`.

## Where things land

State is under `<state root>/<cell>/skunkworks/<run_id>/`: request, state, ideation, research, architecture and assessment as JSON. The state root is `$SWARPH_STATE`, else `$SWARPH_STATE_ROOT`, else `~/swarph_state`. Nothing is written to a repo.

On the card: one typed `status` post per stage (run id, stage, jobs used), then the ideation and the architecture. A post longer than 200,000 characters is cut with a note giving the file path.

## What approval means

Approval produces no code. A build still needs its own spec, plan, row and review.
