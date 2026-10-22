import type { EngineInterface, Register } from 'claude-code'

// Card #1224. Shows ONLY the oldest open commander row above the prompt, from a local cache.
// The render path reads one file. It never calls the network or the swarph CLI; refresh.py does that.
// A press PROPOSES: one DM through this pane's own peer token, answering lab-ovh's ask for the row.
// It closes nothing; the commander confirms on the board under their own session.

type Option = { label: string; text: string; outcome: string }
type Row = { id: number; card_id: number; created_at: string; title: string; options: Option[] | null; ask_id?: number | null; proposed?: string | null }

const CACHE = '.swarph-panel-rows.json'

// Windows sets USERPROFILE, not HOME. Same two lines as ponytail's band.
const home = async ($: EngineInterface) => (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE'))
const claudeDir = async ($: EngineInterface) => (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${await home($)}/.claude`

// The cache is a file on disk: no control characters reach the terminal.
const clean = (s: string) => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')

// Debounce (card #1224, build #2437): the row a successful press proposed for. Module scope, so
// it survives re-renders of this panel for the rest of the session; a press on that same row
// sends nothing again until the panel shows a different row. Measured need: 4 'panel: Yes on
// #2436' DMs in 30 s (card thread 77416). A failed send never sets it, so the row stays pressable.
let proposedFor: number | null = null

// A missing, unreadable or malformed cache is zero rows: the panel draws nothing.
async function readCache($: EngineInterface): Promise<{ rows: Row[]; boardUrl: string; peer: string }> {
  try {
    const cache = JSON.parse(await $.fs.read(`${await claudeDir($)}/${CACHE}`))
    const rows: Row[] = (Array.isArray(cache.rows) ? cache.rows : []).filter(
      (r: Row) => typeof r?.title === 'string' && Number.isFinite(Date.parse(r.created_at))
    )
    rows.sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))
    return { rows, boardUrl: typeof cache.board_url === 'string' ? cache.board_url : '', peer: typeof cache.peer === 'string' ? cache.peer : '' }
  } catch {
    return { rows: [], boardUrl: '', peer: '' }
  }
}

// Outside the render path, and not awaited by the caller: a slow gateway must never hold up a turn.
// shortcut: root from CLAUDE_PLUGIN_ROOT or import.meta.url (a path with spaces would stay percent-encoded); neither is verified in a live CLI.
async function refresh($: EngineInterface, args: string[] = []) {
  try {
    const root = await $.env.get('CLAUDE_PLUGIN_ROOT')
    const script = root ? `${root}/hooks/panel/refresh.py` : new URL('./refresh.py', import.meta.url).pathname
    await $.process.run(['python3', '-I', script, ...args])
  } catch {
    // The old cache stays; the next event tries again.
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    void refresh($)
    return next(e)
  }).catch(($, e, next) => next(e))

  // A subagent's turn is not the main turn.
  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) void refresh($)
    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const { rows, boardUrl, peer } = await readCache($)
    const row = rows[0]
    if (row === undefined || e.props.hasSurvey) return next(e)
    // Other mods share this slot: draw theirs below ours instead of replacing it.
    const below = await next(e).catch(() => undefined)

    // The debounce lock names the row on screen: once the panel shows a different row the
    // lock is spent, even if this render never gets a press.
    if (row !== undefined && proposedFor !== null && row.id !== proposedFor) proposedFor = null
    // A press posts ONE proposal DM and closes nothing (see README "Tap path"): from this pane's own peer, to the
    // commander, reply_to = lab-ovh's ask for the row. No commander credential is held or needed.
    // Only a clean exit counts as proposed; a thrown error or a non-zero exit toasts that nothing was sent.
    // A press on the row already proposed for sends nothing (the debounce above) and says so.
    const tap = async (o: Option) => {
      if (proposedFor === row.id) return $.ui.toast(`already proposed for #${row.id}`)
      const sent = await $.process
        .run(['swarph', 'mesh', 'send', 'commander', '--as', peer, '--kind', 'question', '--reply-to', String(row.ask_id), '--content', `panel: ${clean(o.label)} on #${row.id}`])
        .then(r => (r as { exitCode?: number } | undefined)?.exitCode ?? 0)
        .catch(() => 1)
      if (sent !== 0) return $.ui.toast('swarph-panel: the proposal was not sent; answer it on the board')
      proposedFor = row.id
      $.ui.toast('proposed, confirm on the board')
      // --min-age 0: the cache is younger than refresh.py's default 60 s skip.
      void refresh($, ['--min-age', '0'])
    }
    // shortcut: xdg-open only (Linux), and only an https address from the cache.
    const board = () =>
      /^https:\/\//.test(boardUrl)
        ? $.process.run(['xdg-open', boardUrl]).catch(() => $.ui.toast('swarph-panel: could not open the board'))
        : $.ui.toast(`swarph-panel: card #${row.card_id} row #${row.id} waits on the commander; answer it on the board`)

    // Digit hotkeys 1..9 (build #2437): key N presses option N with no Ctrl+X/Tab focus chord and
    // no mouse — clicks never reach this panel in the classic renderer (card thread 77143-77147).
    // Buttons need an ask to answer and a peer to send as; a cache without them keeps "open board".
    const canPropose = Number.isInteger(row.ask_id) && Number.isInteger(row.id) && peer !== ''
    const options = canPropose && Array.isArray(row.options) && row.options.length > 0 && row.options.every(o => typeof o?.label === 'string') ? row.options : null
    const { Box, Text, Button } = $.ui.resolve(e)
    const panel = (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Text bold>{clean(row.title)}</Text>
        <Box flexDirection="row" alignItems="center" gap={1}>
          {options ? (
            options.map((o, i) => <Button key={`opt-${i}`} label={clean(o.label)} {...(i < 9 ? { hotkey: String(i + 1) } : {})} onPress={() => tap(o)} />)
          ) : (
            <Button key="board" label="open board" onPress={board} />
          )}
          <Box flexGrow={1} />
          <Text dimColor>{`1 of ${rows.length} open`}</Text>
        </Box>
      </Box>
    )
    return below ? <Box flexDirection="column">{panel}{below}</Box> : panel
  }).catch(($, e, next) => next(e))
}
