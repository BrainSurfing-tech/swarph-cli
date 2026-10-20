import { expect, test } from 'claude-code/testing'

// The harness is `claude plugin test plugins/swarph-panel` (same as ponytail's band test).
const PANEL = {
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 100, scroll: { offset: 0, bodyRows: 12 }, view: {} },
} as const
const CACHE_PATH = '/home/u/.claude/.swarph-panel-rows.json'
const YES_NO = [
  { label: 'Yes', text: 'yes', outcome: 'pass' },
  { label: 'No', text: 'no', outcome: 'fail' },
]
// ask_id is lab-ovh's ask for the row; a press answers it. The cache also names the peer the press sends as.
const row = (id: number, created_at: string, title: string, options: unknown = YES_NO, ask_id: number | null = 900 + id) => ({ id, card_id: 1000 + id, created_at, title, options, ask_id, proposed: null })
const cacheOf = (rows: unknown[], extra: Record<string, unknown> = {}) => ({ peer: 'lab-ovh', rows, ...extra })

// Out of created_at order on purpose: the module sorts, the cache does not have to.
const THREE = [
  row(3, '2026-10-19T09:00:00+00:00', 'Newest question?'),
  row(1, '2026-10-19T07:00:00+00:00', 'Extend, flip, or kill?', [
    { label: 'Extend', text: 'extend', outcome: 'pass' },
    { label: 'Flip', text: 'flip', outcome: 'pass' },
    { label: 'Kill', text: 'kill', outcome: 'pass' },
  ]),
  row(2, '2026-10-19T08:00:00+00:00', 'Middle question?'),
]

// A cache file, a prompt below us, and a log of every call that could leave the machine.
function setup(on: any, cache: unknown, extra: Record<string, string> = {}, sendExit = 0) {
  const env: Record<string, string> = { HOME: '/home/u', ...extra }
  on('env.get', ($: any, e: any) => ({ value: env[e.name] }))
  on('fs.read', ($: any, e: any, next: any) => (e.path === CACHE_PATH ? (cache === undefined ? { deny: 'missing' } : { value: typeof cache === 'string' ? cache : JSON.stringify(cache) }) : next(e)))
  // What Claude Code draws without the panel.
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return <Text>prompt only</Text>
  })
  const calls: string[] = []
  const runs: string[][] = []
  const toasts: string[] = []
  on('http.fetch', () => (calls.push('http.fetch'), { deny: 'no network in a test' }) as never)
  on('command.run', () => (calls.push('command.run'), { text: '' }))
  on('process.run', ($: any, e: any) => (calls.push(`process.run ${e.argv.join(' ')}`), runs.push(e.argv), { value: { exitCode: e.argv[0] === 'swarph' ? sendExit : 0, stdout: '', stderr: '' } }) as never)
  on('ui.toast', ($: any, e: any) => (toasts.push(e.message ?? e.text ?? JSON.stringify(e)), { value: undefined }))
  return { calls, runs, toasts }
}
const mount = ($: any) => $.ui.mount({ plugin: 'swarph-panel', surface: 'terminal', ...PANEL })

test('3 rows: only the oldest shows, one button per option, "1 of 3 open", and the prompt below', async ($, on) => {
  const { calls } = setup(on, cacheOf(THREE))
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Extend, flip, or kill\?/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /1 of 3 open/ })).toBeDefined()
  expect(await ui.find({ key: 'opt-0' })).toBeDefined()
  expect(await ui.find({ key: 'opt-1' })).toBeDefined()
  expect(await ui.find({ key: 'opt-2' })).toBeDefined()
  expect(await ui.find({ key: 'opt-3' })).toBeUndefined()
  expect(await ui.find({ key: 'board' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /Newest question/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /Middle question/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /prompt only/ })).toBeDefined()
  // The render path made no network call and ran no program.
  expect(calls).toEqual([])
  await ui.unmount()
})

// One test per case: a test may register its handlers only before it first uses $.
for (const [name, cache] of [['0 rows', cacheOf([])], ['a missing cache', undefined], ['a broken cache', '{not json']] as const) {
  test(`${name}: the panel draws nothing and the prompt is untouched`, async ($, on) => {
    const { calls } = setup(on, cache)
    const ui = await mount($)
    expect(await ui.find({ type: 'Text', text: /prompt only/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /open/ })).toBeUndefined()
    expect(await ui.find({ key: 'opt-0' })).toBeUndefined()
    expect(await ui.find({ key: 'board' })).toBeUndefined()
    expect(calls).toEqual([])
    await ui.unmount()
  })
}

test('an unparseable title shows the title and "open board" only, never guessed buttons', async ($, on) => {
  setup(on, cacheOf([row(7, '2026-10-19T07:00:00+00:00', 'Should we ship or hold the release?', null)]))
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Should we ship or hold the release\?/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /1 of 1 open/ })).toBeDefined()
  expect(await ui.find({ key: 'board' })).toBeDefined()
  expect(await ui.find({ key: 'opt-0' })).toBeUndefined()
  await ui.unmount()
})

test('a malformed options field is treated as unparseable, not guessed', async ($, on) => {
  setup(on, cacheOf([row(8, '2026-10-19T07:00:00+00:00', 'Odd?', [{ nope: 1 }] as never)]))
  const ui = await mount($)
  expect(await ui.find({ key: 'board' })).toBeDefined()
  expect(await ui.find({ key: 'opt-0' })).toBeUndefined()
  await ui.unmount()
})

// The expected argv of a press on THREE's oldest row (id 1, ask 901) at option 1 ("Flip").
const PROPOSAL = ['swarph', 'mesh', 'send', 'commander', '--as', 'lab-ovh', '--kind', 'question', '--reply-to', '901', '--content', 'panel: Flip on #1']
const settle = () => new Promise(r => setTimeout(r, 30))

test('a press posts exactly one proposal DM, closes nothing, toasts, and refreshes without blocking', async ($, on) => {
  const { calls, runs, toasts } = setup(on, cacheOf(THREE), { CLAUDE_PLUGIN_ROOT: '/plug' })
  const ui = await mount($)
  await ui.press({ key: 'opt-1' })
  await settle()
  // Exactly one send, with exactly this argv: the pane's own peer, to the commander, answering the ask.
  expect(runs.filter(a => a[0] === 'swarph')).toEqual([PROPOSAL])
  // The only other program is the cache refresh, and it does not skip on the 60 s age rule.
  expect(runs.filter(a => a[0] !== 'swarph')).toEqual([['python3', '-I', '/plug/hooks/panel/refresh.py', '--min-age', '0']])
  // Nothing else could leave the machine: no slash command, no HTTP, no board close or commander call.
  expect(calls.filter(c => !c.startsWith('process.run '))).toEqual([])
  expect(calls.some(c => /close|obligations|board/.test(c.replace(PROPOSAL.join(' '), '')))).toBe(false)
  expect(toasts).toEqual(['proposed, confirm on the board'])
  // The row is still open, so the panel still shows it.
  expect(await ui.find({ type: 'Text', text: /1 of 3 open/ })).toBeDefined()
  await ui.unmount()
})

test('the toast waits for the send: a non-zero exit says nothing was sent and does not refresh', async ($, on) => {
  const { runs, toasts } = setup(on, cacheOf(THREE), { CLAUDE_PLUGIN_ROOT: '/plug' }, 2)
  const ui = await mount($)
  await ui.press({ key: 'opt-1' })
  await settle()
  expect(runs).toEqual([PROPOSAL])
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('not sent')
  expect(toasts[0]).not.toContain('proposed,')
  await ui.unmount()
})

test('control characters in a label never reach the send argv', async ($, on) => {
  const dirty = [{ label: 'Go\u001b[2J', text: 'go', outcome: 'pass' }]
  const { runs } = setup(on, cacheOf([row(4, '2026-10-19T07:00:00+00:00', 'Go?', dirty)]))
  const ui = await mount($)
  await ui.press({ key: 'opt-0' })
  await settle()
  expect(runs.filter(a => a[0] === 'swarph')[0].at(-1)).toBe('panel: Go [2J on #4')
  await ui.unmount()
})

// A row with no ask has nothing to answer; a cache with no peer has nobody to send as; an ask that is not a
// whole number must never reach --reply-to. Each keeps the title and the "open board" fallback, never a button.
for (const [name, cache] of [
  ['a row with no ask', cacheOf([row(5, '2026-10-19T07:00:00+00:00', 'Go?', YES_NO, null)])],
  ['an ask id that is not an integer', cacheOf([row(5, '2026-10-19T07:00:00+00:00', 'Go?', YES_NO, '905' as never)])],
  ['a cache with no peer', { rows: [row(5, '2026-10-19T07:00:00+00:00', 'Go?')] }],
  ['a cache with an empty peer', cacheOf([row(5, '2026-10-19T07:00:00+00:00', 'Go?')], { peer: '' })],
] as const) {
  test(`${name}: title and "open board" only, no buttons, no call`, async ($, on) => {
    const { calls } = setup(on, cache)
    const ui = await mount($)
    expect(await ui.find({ type: 'Text', text: /Go\?/ })).toBeDefined()
    expect(await ui.find({ key: 'board' })).toBeDefined()
    expect(await ui.find({ key: 'opt-0' })).toBeUndefined()
    expect(calls).toEqual([])
    await ui.unmount()
  })
}

// The test harness cannot read the module's source (a test imports only its own files and "claude-code"), so the
// absence of a prompt.submit handler is shown by behaviour: a typed prompt reaches the host's handler untouched
// and the module spends no program, no network, no toast on it.
test('a typed prompt is not read or answered: the module has no prompt.submit handler', async ($, on) => {
  const { calls, toasts } = setup(on, cacheOf(THREE))
  const seen: unknown[] = []
  on('prompt.submit', ($: any, e: any) => (seen.push(e), { text: 'host answer' }) as never)
  expect(await $.prompt.submit({ text: 'hello' } as never)).toEqual({ text: 'host answer' })
  expect(seen).toEqual([{ text: 'hello' }])
  expect(calls).toEqual([])
  expect(toasts).toEqual([])
})

const ONE = [row(7, '2026-10-19T07:00:00+00:00', 'Odd?', null)]
for (const [url, opened] of [['https://board.example.com/', true], ['file:///etc/passwd', false], ['', false]] as const) {
  test(`"open board" with board_url ${JSON.stringify(url)} ${opened ? 'opens it' : 'opens nothing and says so'}`, async ($, on) => {
    const { calls, toasts } = setup(on, cacheOf(ONE, { board_url: url }))
    const ui = await mount($)
    await ui.press({ key: 'board' })
    expect(calls).toEqual(opened ? [`process.run xdg-open ${url}`] : [])
    expect(toasts.length).toBe(opened ? 0 : 1)
    await ui.unmount()
  })
}

test('control characters in a title never reach the terminal', async ($, on) => {
  setup(on, cacheOf([row(9, '2026-10-19T07:00:00+00:00', 'Ship it?\u001b[2J\u0007', YES_NO)]))
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Ship it\? +/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /\u001b/ })).toBeUndefined()
  await ui.unmount()
})

test('the cache refresh runs on session.start and a main turn, not in render, and not for a subagent', async ($, on) => {
  const { calls } = setup(on, cacheOf([]), { CLAUDE_PLUGIN_ROOT: '/plug' })
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))
  const settle = () => new Promise(r => setTimeout(r, 30))
  await $.session.start({ cwd: '/repo', surface: null } as never)
  await settle()
  expect(calls).toEqual(['process.run python3 -I /plug/hooks/panel/refresh.py'])
  await $.turn.complete({ answer: '', durationMs: 1000 } as never)
  await settle()
  expect(calls.length).toBe(2)
  await $.turn.complete({ answer: '', durationMs: 1000, agentId: 'sub-1' } as never)
  await settle()
  expect(calls.length).toBe(2)
})

test('without CLAUDE_PLUGIN_ROOT the script is found next to the module', async ($, on) => {
  const { calls } = setup(on, cacheOf([]))
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/repo', surface: null } as never)
  await new Promise(r => setTimeout(r, 30))
  expect(calls.length).toBe(1)
  expect(calls[0]).toMatch(/^process\.run python3 -I \/.*\/hooks\/panel\/refresh\.py$/)
})
