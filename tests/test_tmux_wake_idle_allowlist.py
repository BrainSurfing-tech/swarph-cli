"""#1100 (card #1010 rework, lab-ovh ruling_allowlist_ok) — IDLE IS AN
ALLOWLIST: PROPERTY TEST AND THE #1084 RENDERS.

science-claude FAILED #1084 at 74bfa09 (the FOURTH fail on PR 534): the
#1083 'positive idle' still carried a DENYLIST slot, and 9 of 10 novel
busy renders read idle and were typed into — '● Compiling 3 files',
'Allow command? (y/n)', '⏺ Generando respuesta…', …; only '⣾
Synthesizing…' held. lab-ovh's ruling accepts the STRICT allowlist: a
bottom-region pane reads idle ONLY when EVERY raw row matches a form a
REAL idle capture has shown; anything else is UNKNOWN and gets base's
600 s trust in the standing AND the answered state. There is NO
denylist — a row matches or the pane is unknown.

THE PROPERTY (accept clause 1): no adversarial bottom-region row can
make an otherwise-idle pane read idle. Two placements, ≥200 strings
each — seeded random unicode plus y/n and approval prompts, other
languages, partial lines, the #1084 renders (included in the corpus but
not the test): (a) the row REPLACES a blank gap row (the status slot
where mid-turn spinners render); (b) the row is INSERTED directly
above the composer. Every one must read NOT-idle (True for a measured
spinner form is fine; False is the FAIL arm).

Clauses 3–5: our OWN unsubmitted wake text is recognised (nudge goes
promptly, never held to the bound); an uncaptured chrome (cursor-win's
idle-shaped ▄▀ rows) reads unknown until fixtured; and after an
ANSWERED wake the next DM types ONLY on a POSITIVE idle read — every
#1078/#1084 busy render replayed in the ANSWERED state gets 0 keys
sooner than base's 600 s (absence of a busy marker does not count).
"""
from __future__ import annotations

import pathlib
import random
import subprocess
import time

import pytest

import swarph_cli.commands.mesh as mesh
import swarph_cli.commands.watchdog as watchdog

_RENDER_DIR = pathlib.Path(__file__).parent / "pane_renders" / "cursor"


def _pane(name: str) -> list[str]:
    """The fixture RAW — blanks preserved (#1100: the gap is geometry)."""
    return (_RENDER_DIR / f"{name}.txt").read_text(encoding="utf-8").splitlines()


#: The 10 #1084 renders science-claude measured at 74bfa09 — each either
#: read idle and was typed into (9), or held (1: '⣾ Synthesizing…').
#: Included in the property corpus (per the accept: "included but not
#: the test") AND replayed in the answered state (clause 5). The
#: '⠘⠆ Running' + tool-output shape is the one the allowlist still
#: reads positively busy — a MEASURED spinner verb — which is correct:
#: busy, zero keys, never idle.
_RENDERS_1084 = (
    "● Compiling 3 files",
    "Allow command? (y/n)",
    "⏺ Generando respuesta…",
    "* Denke nach... (Strg+C zum Stoppen)",
    "| Working 3.2k tokens",
    "✶ Synthesizing… (12s · 1.2k tokens)",
    "[a] Approve [r] Reject",
    "⠘⠆ Running  173.11k tokens",   # measured spinner: True (busy), not idle
    "⣾ Synthesizing…",               # the one that held at 74bfa09
    "▐ Streaming 4.1k tokens…",      # streaming tool block, no status row
)

#: Categorical adversaries: the shapes the #1084 hunt actually threw at
#: the classifier — prompts that wait for a key, other languages'
#: thinking rows, partial/wrapped lines, and chrome-shaped noise.
_CATEGORICAL = (
    "Proceed? [Y/n]", "y/n", "Y", "n", "(y)es / (N)o",
    "Allow command? (y/n)", "[a] Approve [r] Reject [e] Edit",
    "Trust this command? Enter=yes", "Press Enter to continue",
    "Denke nach... (Strg+C zum Stoppen)", "Generando respuesta…",
    "Réflexion en cours… (Échap pour annuler)",
    "Pensando… (Ctrl+C para detener)", "考え中… (Ctrl+Cで停止)",
    "Working 3.2k tokens", "Working", "●", "● ● ●",
    "Compiling 3 files", "Synthesizing… (12s · 1.2k tokens)",
    "a partial line that wraps mid-wo",
    "⏺", "✶", "◐", "◑", "▄▄▄▄▄▄▄▄▄▄", "▀▀▀▀▀▀▀▀▀▀",
    "│ ▏tool output row 1", "┃ Build · 12s",
    "1 task", "1 tasks", "Kimi K3 Max · 83.6%",
    "~", "/tmp/some/path", "  1 task  ",
    "→ Add a follow-up",  # the composer form ITSELF, misplaced into the gap
)


def _corpus() -> list[str]:
    """≥200 adversarial rows: seeded random unicode + the categorical
    set + the 10 #1084 renders. Deterministic — seed 1100."""
    rng = random.Random(1100)
    pool: list[str] = []
    # random unicode: printable-ish codepoints across the ranges a TUI
    # actually renders — letters, accents, CJK, emoji, braille, boxes,
    # arrows, punctuation — 4–40 chars, with random leading/trailing
    # whitespace so the row-form anchoring is exercised too.
    ranges = (
        (0x21, 0x7E), (0xA1, 0x2FF), (0x370, 0x5FF), (0x1E00, 0x1EFF),
        (0x2000, 0x22FF), (0x2500, 0x27BF), (0x2800, 0x28FF),
        (0x3000, 0x30FF), (0x4E00, 0x4EFF), (0x1F300, 0x1F5FF),
    )
    for _ in range(210):
        n = rng.randint(4, 40)
        chars = []
        for _ in range(n):
            lo, hi = rng.choice(ranges)
            chars.append(chr(rng.randint(lo, hi)))
        s = "".join(chars)
        if rng.random() < 0.3:
            s = " " * rng.randint(1, 3) + s
        if rng.random() < 0.3:
            s = s + " " * rng.randint(1, 3)
        if s.strip():
            pool.append(s)
    pool.extend(_CATEGORICAL)
    pool.extend(_RENDERS_1084)
    # de-dup preserves order; the property needs VOLUME, not uniqueness,
    # but a clean corpus makes a failure report readable
    return list(dict.fromkeys(pool))


#: The real idle pane the property mutates — the #794 hunt's capture,
#: byte-for-byte, the ground truth of the allowlist.
_IDLE_BASE = "idle-lin-794-capture"


def _composer_index(raw: list[str]) -> int:
    """The LAST composer row (``→ …``, never a ``▎`` hunk) — plain
    python, no mesh helpers: this file must also RUN RED on 74bfa09,
    where the #1100 symbols do not exist yet."""
    idx = None
    for i, ln in enumerate(raw):
        s = ln.strip()
        if s.startswith("→") and not s.startswith("▎"):
            idx = i
    assert idx is not None, "the idle fixture must show a composer"
    return idx


def _pin(monkeypatch, raw: list[str]) -> None:
    """Serve the pane to the classifier on whichever capture seam the
    head under test reads: #1100 heads read the RAW pane (blanks are
    geometry); 74bfa09 read the flattened non-empty view — and pinning
    THAT seam is what makes the property RED there (the adversarial gap
    row collapses onto the composer's slot row)."""
    if hasattr(mesh, "_capture_pane_lines_raw"):
        monkeypatch.setattr(mesh, "_capture_pane_lines_raw", lambda t, r=raw: r)
    else:
        monkeypatch.setattr(
            mesh, "_capture_pane_lines",
            lambda t, r=raw: [ln for ln in r if ln.strip()])


def _with_gap_row_replaced(base: list[str], row: str) -> list[str]:
    """Placement (a): the adversarial row REPLACES a blank gap row —
    the status slot where mid-turn spinners render (composer−2)."""
    raw = list(base)
    cidx = _composer_index(raw)
    assert cidx >= 4, "the idle fixture must show the full gap"
    assert not raw[cidx - 2].strip(), "the replaced row must be a blank gap row"
    raw[cidx - 2] = row
    return raw


def _with_row_above_composer(base: list[str], row: str) -> list[str]:
    """Placement (b): the adversarial row is INSERTED directly above
    the composer, pushing the chrome down."""
    raw = list(base)
    raw.insert(_composer_index(raw), row)
    return raw


@pytest.mark.parametrize("mutate", [
    _with_gap_row_replaced, _with_row_above_composer,
], ids=["gap-slot", "above-composer"])
def test_no_adversarial_bottom_region_row_reads_idle(monkeypatch, mutate):
    """>>> RED on 74bfa09: the slot was a denylist — '● Compiling 3
    files', 'Allow command? (y/n)' and seven more novel renders read
    idle there and the sink typed into them. <<< THE PROPERTY (#1100
    clause 1): with ≥200 adversarial rows in the status slot, and
    separately inserted above the composer, an otherwise-real idle pane
    NEVER reads idle. A measured spinner form may read busy (True);
    everything else reads unknown (None). False — idle — is the FAIL
    arm."""
    corpus = _corpus()
    assert len(corpus) >= 200
    base = _pane(_IDLE_BASE)
    # sanity: the unmutated capture is positively idle
    _pin(monkeypatch, base)
    assert mesh._agent_running("sac") is False

    for row in corpus:
        raw = mutate(base, row)
        _pin(monkeypatch, raw)
        verdict = mesh._agent_running("sac")
        assert verdict is not False, (
            f"adversarial row {row!r} made an otherwise-idle pane read idle"
        )


def test_the_1084_replays_each_read_not_idle(monkeypatch):
    """Each #1084 render, placed in the status slot of the real idle
    capture, on its own — named in the failure, not buried in the
    property corpus."""
    base = _pane(_IDLE_BASE)
    for row in _RENDERS_1084:
        raw = _with_gap_row_replaced(base, row)
        _pin(monkeypatch, raw)
        assert mesh._agent_running("sac") is not False, row


# ── clause (3): OUR OWN unsubmitted wake text is recognised ─────────────

def test_own_wake_text_composer_reads_not_running(monkeypatch):
    """>>> RED on 74bfa09: the composer holding our unsubmitted wake
    read UNKNOWN and the Enter nudge sat ~600 s behind the bound. <<<
    The composer holding exactly our wake prompt — or the measured
    stacked double (#533) — is OUR text sitting on screen: recognised,
    not-running, so the nudge goes promptly."""
    for composer in ("→ check mesh", "→ check meshcheck mesh"):
        raw = list(_pane(_IDLE_BASE))
        raw[_composer_index(raw)] = composer
        _pin(monkeypatch, raw)
        assert mesh._agent_running("sac") is False, composer


def test_human_draft_composer_still_reads_unknown(monkeypatch):
    """A composer holding anything else — a HUMAN's draft — is not a
    captured idle form and not ours: unknown, held."""
    raw = list(_pane(_IDLE_BASE))
    raw[_composer_index(raw)] = "→ fix the deploy"
    _pin(monkeypatch, raw)
    assert mesh._agent_running("sac") is None


# ── clause (4): uncaptured chrome reads unknown until fixtured ──────────

@pytest.mark.parametrize("render", ("idle-lin-794-capture",
                                    "idle-lin-fresh-capture"))
def test_real_idle_captures_read_idle(monkeypatch, render):
    """#1100 clause 4: the REAL idle captures — the #794 hunt's pane and
    the FRESH cursor-lin capture (2026-10-06, a 3 s chain caught the
    idle window the moment the previous turn ended; 561 consecutive
    identical reads) — read positively idle, byte-for-byte."""
    _pin(monkeypatch, _pane(render))
    assert mesh._agent_running("sac") is False


def test_uncaptured_cursor_win_chrome_reads_unknown(monkeypatch):
    """cursor-win's idle-shaped chrome — the ▄▄▄/▀▀▀ rows and the
    combined task+footer+path row (busy-win-midturn minus the spinner
    and the composer hint) — is a layout NO real idle capture shows:
    unknown until a real capture is fixtured, never guessed idle.
    >>> RED on 74bfa09: this exact shape read idle (science-claude's
    #1084 probe)."""
    raw = [
        "    Tip: Use subagents to parallelize work and preserve context.",
        "▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄",
        "  → Add a follow-up",
        "▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
        "  1 task   Grok 4.7 256K High Fast · 79.9% · 1 file edited"
        "Run Everything   ~\\cursor-win-workspace",
    ]
    _pin(monkeypatch, raw)
    assert mesh._agent_running("sac") is None


# ── clause (5): after an ANSWERED wake, only a POSITIVE idle read types ──

class _StubState:
    def __init__(self):
        self.gateway = "http://gw:8788"
        self.self_name = "cursor-lin"
        self.token = "tok"
        self._ledgers: dict = {}

    def ledger(self, name: str) -> dict:
        return self._ledgers.setdefault(
            name, {"last_delivered_id": 0, "last_delivery_at": 0.0,
                   "consecutive_failures": 0})


@pytest.fixture
def pane(monkeypatch):
    """A scripted tmux: captures return the fixture render (RAW),
    send-keys are recorded, sleeps are free. The REAL classifier and
    deliver run — the detector is under test, not stubbed."""
    calls = []
    holder = {"lines": _pane("busy-pondering")}

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(
                argv, 0, stdout="\n".join(holder["lines"]) + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mesh.subprocess, "run", fake_run)
    monkeypatch.setattr(mesh.time, "sleep", lambda _s: None)
    monkeypatch.setattr(watchdog, "_gateway_unread_count",
                        lambda *a, **k: 1)
    return calls, holder


def _sent(calls) -> int:
    return sum(1 for c in calls if c[:2] == ["tmux", "send-keys"])


_ANSWERED_BUSY = (
    # the #1078 replays (fixtures, raw)
    "busy-editing-spinner", "busy-streaming-partial",
    "busy-running-tool", "busy-reflexion-fr",
    "busy-pondering", "busy-lin-midturn", "busy-win-midturn",
)


#: Renders the allowlist reads POSITIVELY busy (a measured spinner verb
#: or the composer run hint): they defer via the silent #619 mid-turn
#: path — `deferred_reason` set, no hold line. The rest read unknown and
#: defer via the loud #1077 hold.
_POSITIVELY_BUSY = ("busy-editing-spinner", "busy-lin-midturn",
                    "busy-win-midturn")


@pytest.mark.parametrize("render", _ANSWERED_BUSY)
def test_answered_wake_busy_render_gets_zero_keys(pane, capsys, render):
    """lab-ovh msg 62447, clause 5: after an ANSWERED wake (the flag is
    spent, the inbox refilled), the next DM types ONLY on a POSITIVE
    idle read — absence of a busy marker does not count. Every
    #1078/#1084 busy render replayed in the ANSWERED state gets 0 keys
    on the FIRST poll, sooner than base's 600 s."""
    calls, holder = pane
    holder["lines"] = _pane(render)
    state = _StubState()  # answered: no flag, nothing in play
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0
    if render in _POSITIVELY_BUSY:
        # the measured-spinner deferral names its cause on the sink
        assert sink.deferred_reason
    else:
        out = capsys.readouterr().out
        assert "deferring" in out and "zero keys" in out


@pytest.mark.parametrize("row", _RENDERS_1084)
def test_answered_wake_1084_render_gets_zero_keys(pane, capsys, row):
    """The #1084 renders themselves, replayed in the ANSWERED state
    (science-claude's FAIL arm was exactly this): each render sits in
    the status slot of an otherwise-idle pane; the next DM after an
    answered wake gets 0 keys until the pane reads POSITIVELY idle."""
    calls, holder = pane
    base = _pane(_IDLE_BASE)
    holder["lines"] = _with_gap_row_replaced(base, row)
    state = _StubState()
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is None
    assert _sent(calls) == 0
    out = capsys.readouterr().out
    if "⠘⠆ Running" in row:
        # the measured spinner: positively busy, silent #619 deferral
        assert sink.deferred_reason
    else:
        assert "deferring" in out and "zero keys" in out


@pytest.mark.parametrize("render", ("idle-lin", "idle-lin-794-capture",
                                    "idle-lin-fresh-capture"))
def test_answered_wake_positive_idle_still_types(pane, render):
    """The other arm of clause 5: a POSITIVE idle read — every
    bottom-region row a captured form — still types after an answered
    wake. The allowlist must hold the door shut without jamming it."""
    calls, holder = pane
    holder["lines"] = _pane(render)
    state = _StubState()
    sink = mesh.TmuxSink("sac")

    assert sink.deliver(state, [{"id": 9}], 9) is True
    assert _sent(calls) >= 1