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
import ast
import json
import subprocess
import sys
from pathlib import Path

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


def test_unowned_gates_on_the_shared_property_not_the_surface_label(monkeypatch, tmp_path, capsys):
    """The label is documentation; `shared` is the property.

    An earlier form required `r.surface == "(crontab)"`. Renaming the surface for
    readability — a change nobody reviews closely — silently disabled the check and
    the run stayed GREEN. This test renames it on purpose.
    """
    renamed = dict(_cron(_SIX_CELL_CRON), name="user crontab (lab-ovh)")
    rc, out = _run(monkeypatch, tmp_path, [renamed], "science-claude", capsys=capsys)
    assert "UNOWNED" in out, f"check died when the label changed:\n{out}"
    assert rc == 1


def test_unowned_does_not_fire_on_an_unshared_surface(monkeypatch, tmp_path, capsys):
    """A cell's OWN unit needs no --cell to claim it — only a shared surface does."""
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("swarph-monitor-lab.service",
              "ExecStart=swarph monitor start --state-dir ~/swarph_state/lab/mesh-sidecar\n"),
    ], "lab", capsys=capsys)
    assert "UNOWNED" not in out, out
    assert rc == 0


def test_unowned_is_not_limited_to_the_cursor_key(monkeypatch, tmp_path, capsys):
    """Enumerating which keys may rot is the unenumerable-denylist move again."""
    rc, out = _run(monkeypatch, tmp_path, [
        _cron("*/5 * * * * swarph watchdog --state-dir ~/orphan/state\n"),
    ], "lab", capsys=capsys)
    assert "UNOWNED   --state-dir" in out, out
    assert rc == 1


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


def test_declaration_default_is_per_cell(monkeypatch, tmp_path):
    """The tool's own config must not be a shape-3 defect in the shape-3 detector.

    lab-ovh runs five cells under ONE home. A shared cell_expected.json means two
    cells declaring different intentional --state-dir values SILENCE EACH OTHER —
    and the declaration is exactly what separates a choice from rot. Must be
    per-cell before fleet baselines: a baseline taken against a shared file records
    the wrong thing and cannot be reattributed afterwards.
    """
    monkeypatch.setattr(sc.Path, "home", staticmethod(lambda: tmp_path))
    a = sc.default_declaration_path("lab-ovh")
    b = sc.default_declaration_path("science-claude")
    assert a != b, "five cells, one home, one declaration file"
    assert a.name == "cell_expected.lab-ovh.json", a


# ── the baseline must run where the install is what is broken ────────────────
# MEASURED in an empty venv: `python -m swarph_cli.main` and even a direct
# `from swarph_cli.commands.cell_selfcheck import ...` both die on
# ModuleNotFoundError: swarph_mesh, because the package __init__ eagerly imports
# parsers. A pre-migration baseline is needed precisely on cells whose install may
# be part of the problem, so the file must run standalone.

_MODULE = Path(sc.__file__)
_STDLIB = set(sys.stdlib_module_names)


def test_module_imports_are_stdlib_only():
    """Pins standalone-runnability at the source level, with the reason readable.

    A single non-stdlib import here silently breaks `python3 cell_selfcheck.py` on
    every cell that has no install — and it would break it at the moment the tool
    is most needed.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    imported.discard("__future__")
    assert imported <= _STDLIB, f"non-stdlib imports break standalone use: {imported - _STDLIB}"


def test_runs_as_a_bare_file_in_isolated_mode(tmp_path):
    """-I ignores PYTHONPATH and user site-packages: the closest thing to a cell
    with no swarph install. Asserts the ENTRY POINT works, not just the imports."""
    proc = subprocess.run(
        [sys.executable, "-I", str(_MODULE), "--as", "test-cell",
         "--declaration", str(tmp_path / "absent.json")],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode in (0, 1), f"crashed instead of reporting:\n{proc.stderr}"
    assert "cell selfcheck: test-cell" in proc.stdout, proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr


def test_bare_file_reports_missing_identity_rather_than_guessing(tmp_path):
    """No --as and no $SWARPH_SELF must be exit 2 and a message, never a guess:
    a baseline attributed to the wrong cell is worse than no baseline."""
    env = {"PATH": "/nonexistent"}
    proc = subprocess.run([sys.executable, "-I", str(_MODULE)],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "pass --as" in proc.stderr, proc.stderr


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


# droplet's PR #148 review, finding 1: an earlier form required an UPPERCASE first
# char, so a script's own locals read as literal values and `--state-dir "$statedir"`
# reported DRIFT ON TEMPLATE TEXT. His ten tests never pinned it, so a narrower
# reimplementation passed every gate — the cost of tests-as-spec, paid once.
@pytest.mark.parametrize("val", ["$state", "${state_dir}", "$1", "$statedir/x"])
def test_lowercase_and_numeric_shell_vars_are_placeholders(val):
    assert sc.is_placeholder(val) is True


def test_template_unit_does_not_invent_a_cell_named_percent_i(monkeypatch, tmp_path, capsys):
    rows = sc.extract(_unit("swarph-monitor@.service",
                            "ExecStart=swarph monitor start --as %i --state-dir <HOME>/state\n"))
    assert not any(r.owner == "%i" for r in rows), f"invented a cell named %i: {rows}"
    assert not any(r.value in ("%i", "<HOME>/state") for r in rows), rows
