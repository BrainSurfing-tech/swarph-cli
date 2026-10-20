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
const row = (id: number, created_at: string, title: string, options: unknown = YES_NO) => ({ id, card_id: 1000 + id, created_at, title, options })

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
function setup(on: any, cache: unknown, extra: Record<string, string> = {}) {
  const env: Record<string, string> = { HOME: '/home/u', ...extra }
  on('env.get', ($: any, e: any) => ({ value: env[e.name] }))
  on('fs.read', ($: any, e: any, next: any) => (e.path === CACHE_PATH ? (cache === undefined ? { deny: 'missing' } : { value: typeof cache === 'string' ? cache : JSON.stringify(cache) }) : next(e)))
  // What Claude Code draws without the panel.
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return <Text>prompt only</Text>
  })
  const calls: string[] = []
  const toasts: string[] = []
  on('http.fetch', () => (calls.push('http.fetch'), { deny: 'no network in a test' }) as never)
  on('command.run', () => (calls.push('command.run'), { text: '' }))
  on('process.run', ($: any, e: any) => (calls.push(`process.run ${e.argv.join(' ')}`), { value: { exitCode: 0, stdout: '', stderr: '' } }) as never)
  on('ui.toast', ($: any, e: any) => (toasts.push(e.message ?? e.text ?? JSON.stringify(e)), { value: undefined }))
  return { calls, toasts }
}
const mount = ($: any) => $.ui.mount({ plugin: 'swarph-panel', surface: 'terminal', ...PANEL })

test('3 rows: only the oldest shows, one button per option, "1 of 3 open", and the prompt below', async ($, on) => {
  const { calls } = setup(on, { rows: THREE })
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
for (const [name, cache] of [['0 rows', { rows: [] }], ['a missing cache', undefined], ['a broken cache', '{not json']] as const) {
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
  setup(on, { rows: [row(7, '2026-10-19T07:00:00+00:00', 'Should we ship or hold the release?', null)] })
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Should we ship or hold the release\?/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /1 of 1 open/ })).toBeDefined()
  expect(await ui.find({ key: 'board' })).toBeDefined()
  expect(await ui.find({ key: 'opt-0' })).toBeUndefined()
  await ui.unmount()
})

test('a malformed options field is treated as unparseable, not guessed', async ($, on) => {
  setup(on, { rows: [row(8, '2026-10-19T07:00:00+00:00', 'Odd?', [{ nope: 1 }] as never)] })
  const ui = await mount($)
  expect(await ui.find({ key: 'board' })).toBeDefined()
  expect(await ui.find({ key: 'opt-0' })).toBeUndefined()
  await ui.unmount()
})

test('a press is a stub: it records nothing, calls nothing, and says so', async ($, on) => {
  const { calls, toasts } = setup(on, { rows: THREE })
  const ui = await mount($)
  await ui.press({ key: 'opt-1' })
  expect(calls).toEqual([])
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('NOT recorded')
  expect(toasts[0]).toContain('row #1')
  // The row is still open, so the panel still shows it.
  expect(await ui.find({ type: 'Text', text: /1 of 3 open/ })).toBeDefined()
  await ui.unmount()
})

const ONE = [row(7, '2026-10-19T07:00:00+00:00', 'Odd?', null)]
for (const [url, opened] of [['https://board.example.com/', true], ['file:///etc/passwd', false], ['', false]] as const) {
  test(`"open board" with board_url ${JSON.stringify(url)} ${opened ? 'opens it' : 'opens nothing and says so'}`, async ($, on) => {
    const { calls, toasts } = setup(on, { board_url: url, rows: ONE })
    const ui = await mount($)
    await ui.press({ key: 'board' })
    expect(calls).toEqual(opened ? [`process.run xdg-open ${url}`] : [])
    expect(toasts.length).toBe(opened ? 0 : 1)
    await ui.unmount()
  })
}

test('control characters in a title never reach the terminal', async ($, on) => {
  setup(on, { rows: [row(9, '2026-10-19T07:00:00+00:00', 'Ship it?\u001b[2J\u0007', YES_NO)] })
  const ui = await mount($)
  expect(await ui.find({ type: 'Text', text: /Ship it\? +/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /\u001b/ })).toBeUndefined()
  await ui.unmount()
})

test('the cache refresh runs on session.start and a main turn, not in render, and not for a subagent', async ($, on) => {
  const { calls } = setup(on, { rows: [] }, { CLAUDE_PLUGIN_ROOT: '/plug' })
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
  const { calls } = setup(on, { rows: [] })
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/repo', surface: null } as never)
  await new Promise(r => setTimeout(r, 30))
  expect(calls.length).toBe(1)
  expect(calls[0]).toMatch(/^process\.run python3 -I \/.*\/hooks\/panel\/refresh\.py$/)
})
