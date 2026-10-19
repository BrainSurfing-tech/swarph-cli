import type { EngineInterface, Register } from 'claude-code'

// Card #1224. Shows ONLY the oldest open commander row above the prompt, from a local cache.
// The render path reads one file. It never calls the network or the swarph CLI; refresh.py does that.

type Option = { label: string; text: string; outcome: string }
type Row = { id: number; card_id: number; created_at: string; title: string; options: Option[] | null }

const CACHE = '.swarph-panel-rows.json'

// Windows sets USERPROFILE, not HOME. Same two lines as ponytail's band.
const home = async ($: EngineInterface) => (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE'))
const claudeDir = async ($: EngineInterface) => (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${await home($)}/.claude`

// The cache is a file on disk: no control characters reach the terminal.
const clean = (s: string) => s.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')

// A missing, unreadable or malformed cache is zero rows: the panel draws nothing.
async function readCache($: EngineInterface): Promise<{ rows: Row[]; boardUrl: string }> {
  try {
    const cache = JSON.parse(await $.fs.read(`${await claudeDir($)}/${CACHE}`))
    const rows: Row[] = (Array.isArray(cache.rows) ? cache.rows : []).filter(
      (r: Row) => typeof r?.title === 'string' && Number.isFinite(Date.parse(r.created_at))
    )
    rows.sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at))
    return { rows, boardUrl: typeof cache.board_url === 'string' ? cache.board_url : '' }
  } catch {
    return { rows: [], boardUrl: '' }
  }
}

// Outside the render path, and not awaited by the caller: a slow gateway must never hold up a turn.
// shortcut: root from CLAUDE_PLUGIN_ROOT or import.meta.url (a path with spaces would stay percent-encoded); neither is verified in a live CLI.
async function refresh($: EngineInterface) {
  try {
    const root = await $.env.get('CLAUDE_PLUGIN_ROOT')
    const script = root ? `${root}/hooks/panel/refresh.py` : new URL('./refresh.py', import.meta.url).pathname
    await $.process.run(['python3', '-I', script])
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
    const { rows, boardUrl } = await readCache($)
    const row = rows[0]
    if (row === undefined || e.props.hasSurvey) return next(e)
    // Other mods share this slot: draw theirs below ours instead of replacing it.
    const below = await next(e).catch(() => undefined)

    // STUB (card #1224). Recording a tap as the commander needs the commander's Meta-Edge SSO session
    // (POST /messages as commander, then lab-ovh's relay close); this panel holds no such credential
    // by design. See README "Tap path". The press therefore records nothing and says so.
    const tap = (o: Option) => $.ui.toast(`swarph-panel: "${clean(o.label)}" on row #${row.id} was NOT recorded (no commander credential); answer it on the board`)
    // shortcut: xdg-open only (Linux), and only an https address from the cache.
    const board = () =>
      /^https:\/\//.test(boardUrl)
        ? $.process.run(['xdg-open', boardUrl]).catch(() => $.ui.toast('swarph-panel: could not open the board'))
        : $.ui.toast(`swarph-panel: card #${row.card_id} row #${row.id} waits on the commander; answer it on the board`)

    const options = Array.isArray(row.options) && row.options.length > 0 && row.options.every(o => typeof o?.label === 'string') ? row.options : null
    const { Box, Text, Button } = $.ui.resolve(e)
    const panel = (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Text bold>{clean(row.title)}</Text>
        <Box flexDirection="row" alignItems="center" gap={1}>
          {options ? (
            options.map((o, i) => <Button key={`opt-${i}`} label={clean(o.label)} onPress={() => tap(o)} />)
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
