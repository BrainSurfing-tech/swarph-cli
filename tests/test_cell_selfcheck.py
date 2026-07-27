"""`swarph cell selfcheck` — does this cell agree with itself about where its state lives?

Board card #133. The layer CI structurally cannot see: a cell's own systemd units,
crontab lines and scripts. CI reads the package; only a check running ON a cell can
read that cell's surfaces.

    RESTART FROM ABOVE, READ FROM WITHIN.
Supervision must live above the thing supervised (you cannot restart yourself —
the monitor is a system unit so it survives tmux dying). INSPECTION must live
inside (only a cell can read its own crons). Those look opposed and are not.

SCOPE — gpt-ops's constraint, deliberately narrow: "a narrow read-only baseline
gate with the five known fixtures. Do not let it become an open-ended platform
before it can produce pre/post evidence." This exists to produce a PRE-MIGRATION
BASELINE for #132/#130, so that after the fleet migration a diff separates
migration breakage from pre-existing rot. Nothing more.

THE FIVE SHAPES, all MEASURED on live cells 2026-07-27, each of which broke a
working version of droplet's prototype before it was fixed:
  1 grok-researcher  --cursor outside --state-dir      -> RELATION must fire
  2 lab + drop       a liveness MARKER in --cursor     -> must NOT fire (2 false pos)
  3 lab-ovh          one crontab, six cells            -> per-line ownership
  4 lab-ovh          `--cursor =` empty value          -> MALFORMED, not next-flag
  5 droplet          inactive AND disabled unit        -> FOSSIL, never drift

EVERY TEST HERE RUNS FROM A STRING FIXTURE. NO LIVE BOX, NO systemctl, NO crontab.
A test that only passes on one box is a test that will be deleted by whoever cannot
run it. `live` is therefore a FIELD on the surface dict, never a syscall — otherwise
shape 5 is untestable anywhere but droplet.

Prototype + fixtures: droplet (DM #9161/#9162), corrected twice against
science-claude's box. Implementation is lab's.

Run: venv/bin/python -m pytest tests/test_cell_selfcheck.py -v
"""
import json

import pytest

from swarph_cli.commands import cell_selfcheck as sc


def _unit(name, text, live=True):
    return {"name": name, "kind": "unit", "text": text, "live": live}


def _cron(text):
    return {"name": "(crontab)", "kind": "cron", "text": text, "live": True, "shared": True}


def _run(monkeypatch, tmp_path, surfaces, self_name, expected=None, capsys=None):
    monkeypatch.setattr(sc, "discover_surfaces", lambda: surfaces)
    decl = tmp_path / "cell_expected.json"
    if expected is not None:
        decl.write_text(json.dumps(expected), encoding="utf-8")
    rc = sc.run_selfcheck(self_name=self_name, declaration=decl)
    return rc, capsys.readouterr().out


# ── 1. grok-researcher: --cursor does not live inside --state-dir ────────────

def test_relation_fires_on_cursor_statedir_mismatch(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, [
        _cron("*/5 * * * * swarph watchdog --check --cell grok-researcher "
              "--cursor ~/swarph_state/grok-researcher/cursor.json\n"),
        _unit("swarph-monitor-grok.service",
              "ExecStart=swarph monitor start --as grok-researcher "
              "--state-dir ~/swarph_state/grok-researcher/mesh-sidecar\n"),
    ], "grok-researcher", capsys=capsys)
    assert "RELATION BROKEN" in out, out
    assert rc == 1
    # THE WHOLE POINT OF THE SHAPE: every per-key line reads OK while the
    # relation between two keys is broken. A per-key checker reports all-clear.
    assert "OK        --cursor" in out, "per-key must still read OK — that is the finding"


# ── 2. lab + drop-on-meta-edge: a liveness MARKER passed to --cursor ─────────

def test_marker_in_cursor_is_not_a_relation_break(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, [
        _cron("*/5 * * * * swarph watchdog --check --cell lab "
              "--cursor /tmp/lab-claude-active.txt\n"),
        _unit("swarph-monitor-lab.service",
              "ExecStart=swarph monitor start --as lab "
              "--state-dir ~/swarph_state/lab/mesh-sidecar\n"),
    ], "lab", capsys=capsys)
    assert "RELATION BROKEN" not in out, f"false positive on a liveness marker:\n{out}"
    assert rc == 0, out


@pytest.mark.parametrize("path,kind", [
    ("/tmp/lab-claude-active.txt", "marker"),
    ("/tmp/drop-on-meta-edge-claude-active.txt", "marker"),
    ("~/swarph_state/gridiron/mesh-sidecar/cursor.json", "dm-cursor"),
    ("~/gpt-ops/state/mesh-sidecar/cursor.json", "dm-cursor"),
    ("/var/lib/whatever/blob.dat", "unknown"),
])
def test_cursor_types_classified(path, kind):
    """`unknown` is REPORTED, never guessed — a guess here manufactures drift."""
    assert sc.cursor_type(path) == kind


# ── 3. lab-ovh: ONE crontab, SIX cells, owned by none of them ────────────────

_SIX_CELL_CRON = (
    "*/5 * * * * swarph watchdog --cell science-claude --cursor ~/swarph_state/science-claude/mesh-sidecar/cursor.json\n"
    "*/5 * * * * swarph watchdog --cell gridiron       --cursor ~/swarph_state/gridiron/mesh-sidecar/cursor.json\n"
    "*/5 * * * * swarph watchdog --cell grok-researcher --cursor ~/swarph_state/grok-researcher/cursor.json\n"
    "*/5 * * * * swarph watchdog --cell gpt-ops        --cursor ~/gpt-ops/state/mesh-sidecar/cursor.json\n"
    "*/5 * * * * swarph watchdog --cursor ~/some/orphan/cursor.json\n"
)


def test_shared_surface_ownership_is_per_line():
    owners = {r.owner for r in sc.extract(_cron(_SIX_CELL_CRON)) if r.owner}
    assert owners == {"science-claude", "gridiron", "grok-researcher", "gpt-ops"}, owners


def test_unowned_line_on_a_shared_surface_is_drift(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, [_cron(_SIX_CELL_CRON)],
                   "science-claude", capsys=capsys)
    assert "UNOWNED" in out, out
    assert "~/some/orphan/cursor.json" in out
    assert rc == 1, "a line nobody owns is drift — nobody will notice when it rots"


def test_other_cells_lines_are_visible_but_attributed(monkeypatch, tmp_path, capsys):
    _, out = _run(monkeypatch, tmp_path, [_cron(_SIX_CELL_CRON)],
                  "science-claude", capsys=capsys)
    assert "OTHER CELL" in out
    assert "[gridiron]" in out, "a shared file must never be silently half-checked"


# ── 4. lab-ovh: `--cursor =` with an empty value ─────────────────────────────

def test_empty_flag_value_is_malformed_not_a_swallowed_next_flag():
    rows = sc.extract(_unit("swarph-watchdog-science.service",
                            "ExecStart=swarph watchdog --cursor = --cell science-claude\n"))
    assert any(r.key == "cursor" and r.value == "<EMPTY>" for r in rows), rows
    # the naive-regex failure this prevents: reporting cursor="--cell" confidently
    assert not any(r.value == "--cell" for r in rows), rows


# ── 5. droplet: a unit that is inactive AND disabled ─────────────────────────

def test_dead_unit_is_fossil_never_drift(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("swarph-watchdog.service",
              "ExecStart=swarph watchdog --gateway http://lab-ovh:8788\n", live=False),
        _unit("swarph-monitor-droplet.service",
              "ExecStart=swarph monitor start --as droplet "
              "--state-dir /var/lib/swarph/droplet-monitor "
              "--gateway http://100.107.222.72:8788\n"),
    ], "droplet", capsys=capsys)
    assert "FOSSIL" in out, out
    assert "http://lab-ovh:8788" in out, "a dead surface must be REPORTED, not dropped"
    assert rc == 0, "a fossil configures nothing — it must never be counted as drift"


def test_malformed_on_a_dead_surface_is_reported_but_not_drift(monkeypatch, tmp_path, capsys):
    """science-claude's find, and the strongest case for reporting fossils rather
    than dropping them: a dead surface can still be corrupt, and that is evidence."""
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("swarph-watchdog-science.service",
              "ExecStart=swarph watchdog --cursor = --cell science-claude\n", live=False),
    ], "science-claude", capsys=capsys)
    assert "MALFORMED" in out, out
    assert rc == 0, "malformed on a DEAD surface is not drift; on a live one it is"


# ── declaration semantics: the product is telling CHOICE from ROT ────────────

def test_declared_divergence_is_not_drift(monkeypatch, tmp_path, capsys):
    surfaces = [
        _unit("a.service", "ExecStart=x --state-dir /var/lib/swarph/droplet\n"),
        _unit("b.service", "ExecStart=x --state-dir /var/lib/swarph/droplet-monitor\n"),
    ]
    rc_undeclared, _ = _run(monkeypatch, tmp_path, surfaces, "droplet", capsys=capsys)
    assert rc_undeclared == 1, "two values, nothing declared -> drift"

    rc_declared, out = _run(monkeypatch, tmp_path, surfaces, "droplet", expected={
        "state-dir": ["/var/lib/swarph/droplet", "/var/lib/swarph/droplet-monitor"]},
        capsys=capsys)
    assert rc_declared == 0 and "DECLARED" in out, out


def test_the_scan_can_actually_fail():
    """A guard that cannot fail is not a guard. Pins the relation logic itself."""
    assert sc.relation_broken("~/s/x/cursor.json", "~/s/x/mesh-sidecar") is True
    assert sc.relation_broken("~/s/x/mesh-sidecar/cursor.json", "~/s/x/mesh-sidecar") is False


# ── 6. lab-ovh: a systemd TEMPLATE unit — %i is not a cell ───────────────────
# Found by running the tool live on lab-ovh, which has swarph-monitor@.service.
# droplet's five fixtures could not cover this: his box has no template unit.
# The per-cell check found a shape the prototype's author structurally could not.

@pytest.mark.parametrize("val", ["%i", "%n", "$HOME", "${PEER}", "<PEER>"])
def test_template_placeholders_are_not_values(val):
    assert sc.is_placeholder(val) is True


@pytest.mark.parametrize("val", ["lab-ovh", "/var/lib/swarph/x", "100%real"])
def test_real_values_are_not_placeholders(val):
    assert sc.is_placeholder(val) is False


def test_template_unit_does_not_invent_a_cell_named_percent_i(monkeypatch, tmp_path, capsys):
    rows = sc.extract(_unit("swarph-monitor@.service",
                            "ExecStart=swarph monitor start --as %i --state-dir <HOME>/state\n"))
    assert not any(r.owner == "%i" for r in rows), f"invented a cell named %i: {rows}"
    assert not any(r.value in ("%i", "<HOME>/state") for r in rows), rows
