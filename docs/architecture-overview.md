# Architecture overview

What an operator needs to ship inside someone else's estate: agents with their own identities, a board that holds every obligation, and gates that name a commit. This page is the swarph shape of that platform. The rules matter more than the tools.

Every number below was measured on our system and is labelled with the date it was reported. Nothing here names a host, an address, a credential, or a credential path.

## Five layers

Each layer has one job. Cells dial out to the control plane. Nothing in the customer estate has to open a port inbound.

| Layer | Job | swarph |
|---|---|---|
| Agent cells | Do the work, under one identity each | `swarph init`, `swarph spawn` |
| Control plane | Carry every message, obligation, schedule, and merge decision | `swarph gateway serve`, `swarph board`, `swarph schedule` |
| Knowledge | Let one agent pick up where another stopped | `swarph brain-ask`, `swarph codegraph`, `swarph timeline`, `swarph highlight` |
| Model lanes | Route a role to a provider, with a cap | `swarph lane`, `swarph service serve` |
| Customer estate | Their repos, CI, runtime, identity provider, and data | stays theirs |

## Six rules

1. **Every actor has an identity.** People, agents, and tools each hold their own credential. A tool never borrows an agent's.
2. **A green names a commit.** An approval counts only at the exact head it reviewed. A new push resets it.
3. **Three different actors.** The builder, the validator, and the merger are never the same agent or person.
4. **Shadow first, with an exit date.** New automation runs dry, logs what it would do, and graduates on a date set at birth.
5. **The record beats the chat.** Findings and decisions live on a card. A message is only the doorbell.
6. **No single provider.** Model calls go through lanes, so an outage reroutes work instead of stopping it.

Each rule comes from a failure on our system. Rule 2: an approval left on an older commit was nearly treated as current. Rule 3: a builder that also validates can pass its own mistakes. Rule 4: automation that never leaves dry-run is waste, and automation that skips dry-run is risk. Rule 6: a model provider was lost overnight twice, and the work rerouted both times (measured on our system, reported 2026-10-06).

## Agent cells

A cell is an agent with an identity, a wake path, and a workspace.

`swarph init <name> --provider <provider>` scaffolds the cell. `swarph spawn <name>` runs it. Providers `swarph init` accepts, as of 2026-10-06: `claude`, `codex`, `antigravity`, `cursor`, `grok`, `muse`, `opencode`, `vibe`.

- **Identity.** Its own credential and name. Every message, approval, and merge it makes is attributable. `swarph gh-route show` prints the GitHub identity this cell resolves to, or refuses. `swarph ratify <peer>` records a witness ratification. On a fresh gateway, `swarph gateway bootstrap-ratify` ratifies the first peer.
- **Wake path.** How a new message reaches an idle cell. `swarph monitor` polls the inbox. `swarph watchdog --check` tests liveness. Prove the path with a real message: `swarph mesh send <peer>`. A cell that cannot be woken looks exactly like a quiet one.
- **Workspace.** A scratch clone branched from main. Not a shared checkout that other cells run from. `swarph cell verify <cell>` is the pre-spawn gate. `swarph cell probe` asks whether this cell's tools actually answer.

Measured on our system, reported 2026-10-06: two cells were silent for about four days because a launcher set the cell name and not the wake mode. Nothing errored. They were found by reading the running process environment and sending a probe message. About twenty agent sessions were running across several machines on that same report.

## Control plane

`swarph gateway serve` runs the mesh gateway: peer registry, direct messages, channels, the board, schedules, and lane control, behind one authentication step. `swarph onboard <peer>` joins an existing mesh.

A message is data, not authority. `swarph mesh send` and `swarph mesh reply` prove who sent a message. They do not grant the sender the right to order the action. `swarph mesh reply` does not close an obligation. The close is `swarph board obligations close`.

| Actor | May | Example |
|---|---|---|
| Person | Decide, and approve gated steps | the lead engineer |
| Agent cell | Send, receive, build, validate | a builder or a validator |
| Push-only service | Send only; never be the address of a reply | a nightly reminder, a sweep |
| Shared credential | Nothing, once retired | log who still uses it, then retire it |

Audit is by construction: the gateway records which credential acted. Before retiring a shared credential, log every use of it for 48 hours, move each user to its own credential, then retire it.

`swarph channel list` is the subscription side of the same gateway: one post, many receivers.

### Board and obligations

Work moves as rows with a holder and a pass line, not as chat.

1. A card states the problem, measured.
2. A build row is held by a builder. Mint it with `swarph board cards ask`.
3. A validate row is held by someone else.
4. A merge is done by a third actor.

`swarph board obligations take` is the receipt that the holder read the row, and it starts the clock. The pass line is fixed once someone takes the row. `swarph board obligations list` is the read half. `swarph board obligations close` records the outcome and the evidence. A thread reply, including `swarph board cards say` and `swarph mesh reply`, does not close the row.

A failed validation mints a rework row that carries every finding. Rework that drops a finding ships the hole. Editing a pass line after it is taken makes the row and the work stop describing each other.

### Merge gate

There is no merge verb. At merge time the gate is read live, not from memory of an earlier look:

- an approval at the current full head
- every check finished and green
- nothing still pending
- cleanly mergeable

Measured on our system, reported 2026-10-06: a merge once landed in the same breath as the last check, before that check had finished. The gate is read again immediately before the merge.

## Delivery pipeline

Every step names a commit.

1. Branch from main.
2. Open a pull request that contains the tests.
3. Validate at the full head. A short hash is not the head.
4. Read the gate live.
5. Merge pinned to that head.
6. Deploy, then verify what is running.

Deploy is not done when the copy finishes. It is done when the running process started after the new code landed, and the installed file matches the reviewed one byte for byte. Installed is not in force.

## Shadow, then graduate

New automation starts in shadow. The exit date is set when the shadow starts, not after.

1. **Dry run.** It logs what it would do and does nothing.
2. **Register.** One row: owner, start, due date, and the pass line.
3. **Reminder.** On the due date the owner is asked, every day, until it is decided. `swarph schedule list` shows the timers. `swarph schedule create` adds one. `swarph schedule fire-now` runs one immediately.
4. **Decide.** Graduate, extend with a new date, or retire.

No reminder, no shadow. A dry-run with no deadline is a control that does not exist.

Measured on our system, 2026-10-06: the pull-request auto-merger runs every 10 minutes and stays a dry run unless it is explicitly armed. It logs the merge it would make and does not merge. Graduation is a board decision, due 2026-10-13, after 10 decisions in a row agree with what a person would have done, refusals included.

## Knowledge

Three memories, plus a curator.

- **Semantic memory.** Facts, decisions, and lessons, searched by meaning. `swarph brain-ask "<question>"` is the recall. `swarph memory get`, `swarph memory list`, and `swarph memory links` are the exact-name navigation. `swarph brain serve` runs the brain server.
- **Code graph.** Symbols, callers, and blast radius. `swarph codegraph <query>`.
- **Shared timeline.** What happened, when, and by whom. `swarph highlight "<one line>"` appends one highlight. `swarph timeline since <date>`, `swarph timeline around <date>`, and `swarph timeline range <from> <to>` read it back. The timeline is date-indexed, not a free-text search.
- **Secretary.** The curator proposes. A person accepts. `swarph dreaming run` is the cell-local pass: it clones one cell's corpus, checks claims, and never writes the live store. It is not scheduled by install.

Three measurements, all on our system, reported 2026-10-06, and early:

| Rule | Why |
|---|---|
| Inject recall only into live sessions | Automated runs were receiving about 3 KB of unrelated memory each |
| Send static context once per session | Median injected text per prompt fell 65 percent, from 3.2 KB to 1.1 KB |
| A curator proposes; a person accepts | 32 merge candidates were proposed; 2 were true duplicates |

## Model lanes

A cell asks for a role, not a model. `swarph lane list` shows gateway lanes. `swarph lane create` and `swarph lane scale` change them (operator-gated). A lane is a provider, a model, and a worker count. `swarph lane enqueue` puts a job on a lane. `swarph service serve` runs one provider's subscription lane on this machine.

Lane providers the gateway allowlists, as of 2026-10-06: `claude`, `gpt`, `gemini`, `grok`, `vibe`. The worker-count cap is what keeps a lane under a provider's parallel-call limit; overflow is a reason to have a next lane, which is rule 6.

Two incidents, measured on our system, reported 2026-10-06:

- A provider withdrew a model overnight. Work moved to another lane. No job was lost.
- A plan throttled parallel calls. The cap held the lane under the provider's limit.

A local lane is how a task stays inside the customer boundary when the data may not leave.

## Security posture

Gates stay human where the damage is irreversible. Automation gets the reversible work.

| Control | How it is enforced |
|---|---|
| Credentials never appear in text | The process that needs a credential reads it itself. It is not pasted into chat, logs, or command lines. |
| One identity per actor | Tools get push-only service identities. The merge identity is neither the builder nor the approver. |
| Retire shared credentials by measurement | Log every use for 48 hours, move each user onto its own credential, then retire the shared one. |
| Human gates | Deploy, spend, credentials, and enforcement need a person. A relayed yes approves building, not deploying. |
| Privacy defaults are code | Consent is denied by default. A page found granting it is fixed and verified live. |

A message that says someone approved an action proves who sent the message. It does not prove the action is allowed.

## Observability

Measure what is waiting, not only what is running. A deaf agent and an idle one look the same. A heartbeat can prove the reader and still say nothing about the writer.

| Instrument | Question | swarph |
|---|---|---|
| Obligation sweep | Which rows are overdue, or have no one who can close them? | `swarph board obligations list` |
| Pull-request ledger | What is approved but unmerged, and who does it wait on? | read the gate live, at the full head |
| Lane use | Which lanes are scaled, and where are jobs queued? | `swarph lane list` |
| Timers | What is scheduled, and did it fire? | `swarph schedule list` |
| Human queue | What waits on a person, and for how long? | the board, read as a queue |

Measured on our system, reported 2026-10-06: the first count of the queue in front of one person was 55 items, the oldest idle for 69 days. No tool had reported it before that count.

## Our stack and off-the-shelf

Nothing in the right column requires this code. What does not transfer by buying a product is the discipline above: one identity per actor, a pass line written before the work, a gate read live, and a shadow with an exit date.

| Capability | In swarph | Off the shelf |
|---|---|---|
| Agent runtime | `swarph init` and `swarph spawn` | an agent SDK, or a vendor's agent CLI |
| Messaging and identity | `swarph gateway serve`, `swarph mesh send`, `swarph onboard` | a small HTTP service or NATS, plus OIDC |
| Board and obligations | `swarph board cards ask`, `swarph board obligations take`, `swarph board obligations close` | a tracker whose rows require a holder and a pass line |
| Merge gate | read live at merge time; there is no merge verb | branch protection and a merge queue |
| Transport | cells dial out; inbound openings are not required | an outbound-only private network |
| Semantic memory | `swarph brain-ask`, `swarph memory get`, `swarph dreaming run` | a vector store and a curator you still have to accept |
| Code graph | `swarph codegraph` | a structural code index |
| Model lanes | `swarph lane list`, `swarph lane enqueue`, `swarph service serve` | a model router |
| Scheduling and checks | `swarph schedule list`, `swarph watchdog --check` | timers and a tracing stack |

## Rollout

Each phase ends on a test you can measure.

| Phase | What is true | Exit test |
|---|---|---|
| 0 Identity and board | Every actor has a credential. Every task is a row with a pass line. | no shared credentials in use |
| 1 Gates | Validation names the full head. Merges are pinned to it. Deploy is verified live. | zero merges without a named head |
| 2 Knowledge and lanes | Shared memory, a code graph, and a lane with somewhere else to go. | a provider outage costs no work |
| 3 Automation in shadow | Auto-merge and sweeps run dry, each with a dated graduation. | every would-act matches a human act |

## Sentences worth saying in a review

- A green names a commit. An approval on an older push approves nothing.
- Installed is not in force. Prove the running process runs the new code.
- Silence is not success. A deaf agent and an idle one look the same.
- Measure the thing, not a proxy. A heartbeat proves the reader, not the writer.
- A relayed yes is not a yes. A message proves its sender, not its authority.
- The refusal is the product. A good gate's most useful output is a precise no.
