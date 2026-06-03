# swarph spawn on Windows — known issues + workarounds

**Status:** active investigation 2026-05-19. No Windows test environment
available to confirm fixes in CI; report covers documented patterns +
hypothesis chain for the specific "Enter returns 'm'" symptom commander
hit on workstation-lc 2026-05-17.

## TL;DR — if you're hitting it now

**Good news: `swarph spawn` now auto-rescues you.** It detects a *genuine*
Windows Terminal by walking the process ancestry (looking for a
`WindowsTerminal.exe` ancestor) — it is **no longer fooled by an inherited
`WT_SESSION`** env var. The default is to **relaunch**: unless it can
positively confirm you're already in a real Windows Terminal, it pops a fresh
WT window (where the Ink TUI works) and tells you the old console can be
closed. It only stays put when it's *confirmed* in a genuine WT.

- `SWARPH_FORCE_WT=1` — force a relaunch even from a genuine Windows Terminal.
- `SWARPH_WIN_ACK=1` — stay in the current console (opt out of relaunch + warning).

> **Why ancestry, not `WT_SESSION`?** Live repro 2026-06-03 on workstation-lc:
> launching from Windows Terminal *sets* `WT_SESSION`, and corporate setups
> *inherit* it into child `conhost` consoles. The old skip heuristic keyed on
> `WT_SESSION` → it wrongly concluded "already in a good WT, no relaunch
> needed" → the user was left stuck on a broken console with **no new window**.
> Process ancestry is ground truth and fixes this.

If the auto-relaunch can't fire (e.g. `wt.exe` isn't installed) and Claude
Code's TUI renders incorrectly **with pressing Enter inserting a literal `m`
character into the input**, try the workarounds in order of decreasing
severity:

1. **Switch terminal emulator from legacy `conhost.exe` to Windows
   Terminal** (the modern Microsoft Store app). Most TUI issues collapse
   here. If you're using PowerShell ISE, switch to Windows Terminal
   regardless — PowerShell ISE doesn't support ANSI escape sequences.
2. **Run swarph spawn from WSL2** instead of native Windows shell.
   Claude Code's TUI ships well-tested on Linux; WSL2 gets the Linux
   build of `claude` on Windows hardware. Tradeoff: filesystem paths
   become Linux-shaped, may affect cell.yaml `cwd:` resolution.
3. **Verify Windows Terminal version ≥ 1.18** + Windows 11. Earlier
   versions had VT100-input bugs that affect Ink-based TUIs.

## The "Enter returns 'm'" hypothesis

The literal symptom: pressing Enter inserts a literal `m` character at
the cursor instead of submitting. Recorded by commander 2026-05-17 on
workstation-lc (Windows-side `swarph spawn` of a claude session).

**Root-cause hypothesis chain** (high confidence on the class, lower
confidence on the specific):

### Hypothesis 1 (most likely): incomplete VT100 escape sequence + SGR terminator

Claude Code's TUI uses [Ink](https://github.com/vadimdemedes/ink) (React
for CLIs). Ink renders styled text via ANSI SGR escape sequences:
`ESC [ <params> m`, where `m` is the SGR terminator. Examples:

- `\x1b[31m` — red foreground
- `\x1b[0m` — reset
- `\x1b[1;33m` — bold yellow

On Windows native console (`conhost.exe` legacy host without
`ENABLE_VIRTUAL_TERMINAL_INPUT` flag), keyboard input arrives as Win32
`INPUT_RECORD` events — NOT VT100 escape sequences. Node's `readline`
adapter on Windows attempts to translate, but the translation is
incomplete for newer TUI libraries that expect bracketed paste / cursor
keys / Enter as proper VT100 sequences.

Specific path to the `m` symptom:

1. Ink writes a styled prompt — TUI is in mid-render with partially-
   buffered SGR sequence in its parser state.
2. Operator presses Enter. Windows sends `\r` (CR) alone — NOT `\r\n`.
3. Ink's input parser is in escape-pending state. `\r` doesn't match a
   recognized escape continuation, so the parser falls back to
   passthrough mode for the buffered sequence.
4. The `m` terminator from the buffered SGR sequence leaks into the
   input buffer as a literal character.
5. The buffered sequence was for Ink's own rendering — it should
   never have hit the input path. But Windows console's bidirectional
   stream + lack of clean VT-input separation lets the output leak
   into input.

**Why this fits commander's symptom specifically:**
- Pressing Enter is the trigger (state-machine transition)
- The leaked character is `m` (SGR terminator)
- Other Windows TUI bugs in claude-code issues (#58579 line stacking,
  #58555 layout corruption, #59899 arrow-key freeze) point at the same
  underlying conhost VT incompatibility class

### Hypothesis 2 (less likely): bracketed-paste mode interaction

Ink may enable bracketed-paste mode (`\x1b[?2004h`) for paste detection.
Windows conhost partially supports this — sends `\x1b[200~` start +
`\x1b[201~` end markers. If a paste happens mid-Enter or terminal mode
is mis-set, Enter could match a partial bracketed-paste end marker
ending in `~`... but symptom is `m` not `~`, so this hypothesis is
weaker.

### Hypothesis 3 (unlikely): keybinding override misfire

claude-code's keybinding system (#60156 — open issue about Enter
override on Windows) could be misfiring on certain Windows shells. But
this would manifest as Enter doing nothing OR doing a different action —
not inserting a literal character.

## Documented Windows issues in claude-code's tracker

These don't exactly match "Enter→m" but are in the same class — Windows
TUI input/rendering misbehavior:

| Issue | Status | Symptom |
|---|---|---|
| #58579 | open | TUI rendering: /agents view lines stack/overlap on Windows Terminal (v2.1.140) |
| #58555 | open | FleetView dashboard layout broken after returning from session (Windows) |
| #58606 | open | Bash/PowerShell tool calls flash visible conhost window |
| #58664 | open | Ctrl-G external editor spawn regression (Windows + Cygwin/MSYS2) |
| #59899 | open | Agents view becomes unresponsive after left arrow key on Windows 11 PowerShell |
| #60156 | open | Cannot override default Enter → chat:submit on Windows |
| #60212 | open | /agents TUI freezes after Esc/arrow-back |

**Common thread:** input event handling on Windows native shells is
fragile. The specific symptom commander hit is plausibly a derivative
of the same underlying VT-input compatibility gap that produces these
other issues.

## What swarph-cli can do about it

### Done in this PR

- This document (`docs/WINDOWS_KNOWN_ISSUES.md`) for future-operator
  recall.
- **Auto-relaunch in Windows Terminal (relaunch-by-default).** `swarph spawn`
  relaunches the `claude` session inside Windows Terminal (where the Ink TUI
  works), then exits the original console — UNLESS it can positively confirm
  it's already in a genuine Windows Terminal. This is the primary fix — it
  sidesteps conhost's VT-input gap entirely rather than trying to patch it.
  See `_relaunch_in_windows_terminal` + `_console_is_genuine_wt` in `spawn.py`.
- **Genuine-WT detection via process ancestry, not `WT_SESSION`.**
  `_console_is_genuine_wt()` walks the parent-process chain (Win32
  `CreateToolhelp32Snapshot` / `Process32First` / `Process32Next`) and returns
  True only if an ancestor process is `WindowsTerminal.exe` (case-insensitive).
  It fail-safes to False on any error or non-Windows platform → which makes the
  caller relaunch (the foolproof direction). This replaces the old
  `WT_SESSION`-env heuristic, which was inherited into child `conhost` consoles
  and set whenever a shell was launched from WT — so it wrongly skipped the
  relaunch and stranded users on a broken console with no new window.
- **Env knobs** controlling that relaunch:
  - `SWARPH_FORCE_WT=1` — **force the relaunch even when already in a genuine
    Windows Terminal.** (The old "needed because `WT_SESSION` is inherited"
    rationale is now obsolete — ancestry detection handles that case
    automatically — but the override is kept for operators who want a fresh WT
    window unconditionally.)
  - `SWARPH_WIN_ACK=1` — opt out of both the relaunch and the warning
    (stay put in the current console).
  - `SWARPH_SPAWN` (internal) — set on the relaunched session; the reliable
    loop-guard so a relaunched session never re-relaunches.
  - The relaunch also no-ops when stdout is not an interactive TTY (CI /
    piped) and when `wt.exe` is absent (then it warns instead).
- Banner update in `swarph spawn`: when it *can't* auto-relaunch (no
  `wt.exe`), it warns and points here.

### Deferred (requires Windows test environment to validate)

- **Windows-aware spawn shape.** Today `swarph spawn` uses `os.execvp`
  (Unix exec-replace pattern). On Windows, `os.execvp` works but has
  different semantics (`CreateProcess` + exit-current-process pattern,
  not in-place replacement). May affect how the parent shell hands off
  stdio to Claude CLI. Code change:
  ```python
  if sys.platform == "win32":
      rc = subprocess.run(argv, check=False).returncode
      sys.exit(rc)
  else:
      os.execvp(argv[0], argv)
  ```
  Untested — would need a Windows machine to validate.

- **`TERM=xterm-256color` injection on Windows.** Some Ink versions
  fall back to safer ANSI subset when TERM is set explicitly. Worth
  trying in Windows-aware `_subprocess_env` if hypothesis 1 holds.

- **VT mode probe at spawn-time.** Check if the inherited stdio
  supports VT input via Win32 `GetConsoleMode` API + flag check. If
  not, surface a stronger warning AND attempt to set
  `ENABLE_VIRTUAL_TERMINAL_INPUT` via `SetConsoleMode` before exec.
  Requires `pywin32` or `ctypes` Win32 bindings.

## Diagnostic steps (for when commander hits it next)

1. **Identify shell first:**
   ```powershell
   # In the failing shell, run:
   $Host.Name                # "ConsoleHost" / "Windows PowerShell ISE" / etc.
   $env:WT_SESSION           # USUALLY set iff Windows Terminal — but NOT
                             # reliable: it is INHERITED into child conhost
                             # consoles spawned from a WT session, so a broken
                             # conhost can carry a non-empty WT_SESSION. If you
                             # see WT_SESSION set but the TUI is still broken,
                             # you're in an inherited-env conhost — use
                             # SWARPH_FORCE_WT=1 to force the WT relaunch.
   ```
   If `Windows PowerShell ISE` — switch immediately. ISE doesn't support
   ANSI at all.

2. **Check TERM env var:**
   ```powershell
   $env:TERM                 # likely empty or unset on Windows native
   ```
   Try setting before spawn:
   ```powershell
   $env:TERM = "xterm-256color"
   swarph spawn <cell>
   ```

3. **Confirm Windows Terminal version** if using it:
   - Open Windows Terminal → Settings → About. Need ≥ 1.18 for stable
     VT input handling.

4. **WSL2 fallback test:** in WSL2 Ubuntu shell, run the same `swarph
   spawn <cell>` to confirm it works there. If WSL2 works + Windows
   native fails, hypothesis 1 (conhost VT-input gap) is essentially
   confirmed.

5. **Strace-equivalent on Windows:** Process Monitor (sysinternals)
   can show stdio activity, but interpreting it requires understanding
   conhost's input pipe shape. Probably not worth it unless we're
   fixing the upstream.

## Upstream-fix path

This is fundamentally an upstream Claude Code (claude-code repo) issue.
The right fix is in their TUI input layer (Ink config or Windows-
specific input adapter). Filing an issue with commander's specific
reproducer ("Enter inserts literal `m`") + a minimal repro script
would help Anthropic engineers track it down. swarph-cli can only
mitigate at the spawn boundary.

If commander files an upstream issue, link it here for cross-reference.

## Cross-references

- swarph-cli `src/swarph_cli/commands/spawn.py` — exec-replace pattern,
  Windows-aware shape deferred
- claude-code GitHub issues (Windows label): https://github.com/anthropics/claude-code/issues?q=label%3Awindows
- Ink TUI library: https://github.com/vadimdemedes/ink
- Windows Terminal docs: https://learn.microsoft.com/en-us/windows/terminal/
