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


# An inert resolver: NO gateway claim, no token, an EMPTY but READ socket
# table. Tests that do not inject this would read the REAL box's env and
# /proc/net/tcp — the "only passes on one box" trap this whole suite exists
# to avoid, one layer out. (PR #261 review: exactly that fall-through made
# test_per_cell_declaration_wins_when_both_exist green on the author's box
# and red on CI — the box's own MESH_GATEWAY_URL leaked into the verdict.)
_INERT_RESOLVER = {
    "gateway": None,
    "token": (None, "no token resolvable"),
    "listeners": set(),
    "socket_table": "read",
}


@pytest.fixture(autouse=True)
def _no_real_resolver(monkeypatch):
    """Hermeticity is not opt-in: ANY call that forgets to inject a resolver
    gets the inert one, never the environment of whatever runner executes
    it. If a fall-through path exists at all, something will take it."""
    monkeypatch.setattr(sc, "resolve_cell_inputs",
                        lambda self_name: dict(_INERT_RESOLVER))


def _run(monkeypatch, tmp_path, surfaces, self_name, expected=None, capsys=None,
         resolver=None):
    monkeypatch.setattr(sc, "discover_surfaces", lambda: surfaces)
    decl = tmp_path / "cell_expected.json"
    if expected is not None:
        decl.write_text(json.dumps(expected), encoding="utf-8")
    rc = sc.run_selfcheck(self_name=self_name, declaration=decl,
                          resolver=resolver or _INERT_RESOLVER)
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

_NEIGHBOUR_BREAK = [
    _cron("*/5 * * * * swarph watchdog --cell grok --cursor ~/s/grok/cursor.json\n"),
    _unit("swarph-monitor-grok.service",
          "ExecStart=swarph monitor start --as grok --state-dir ~/s/grok/mesh-sidecar\n"),
    _unit("swarph-monitor-lab.service",
          "ExecStart=swarph monitor start --as lab --state-dir ~/s/lab/mesh-sidecar "
          "--cursor ~/s/lab/mesh-sidecar/cursor.json\n"),
]


def test_another_cells_relation_break_is_reported(monkeypatch, tmp_path, capsys):
    """RELATIONS ARE FACTS, AND FACTS ARE NOT PER-OWNER.

    Evaluating the relation over self-owned rows only meant a neighbour's live
    divergence sat in the output as an attributed line that nothing checked. DRIFT is
    declaration-dependent and correctly per-owner — another cell's divergence may be
    intentional and declared in a file you cannot read. A cursor outside its state-dir
    is wrong whoever owns it, and NO DECLARATION CAN MAKE IT RIGHT (droplet: this was
    in his prototype, his ten fixtures never pinned it, so the reimplementation
    dropped it and passed both CI and his own review).
    """
    _, out = _run(monkeypatch, tmp_path, _NEIGHBOUR_BREAK, "lab", capsys=capsys)
    assert "RELATION BROKEN[grok]" in out, out


def test_another_cells_relation_break_does_not_fail_my_verdict(monkeypatch, tmp_path, capsys):
    """Their fact, my observation. Failing my run on a neighbour's config would make
    every cell on a shared box un-green until someone else fixes something — and a
    verdict nobody can clear is one everybody learns to ignore."""
    rc, _ = _run(monkeypatch, tmp_path, _NEIGHBOUR_BREAK, "lab", capsys=capsys)
    assert rc == 0, "a neighbour's break is reported, not inherited"


def test_my_own_relation_break_still_fails_me(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, _NEIGHBOUR_BREAK, "grok", capsys=capsys)
    assert "RELATION BROKEN " in out and "[grok]" not in out, out
    assert rc == 1


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
    """`-I -S`: the closest reachable approximation of a cell with no swarph install.

    -I alone drops PYTHONPATH and user-site but LEAVES SYSTEM SITE-PACKAGES; -S
    removes those too (droplet, PR #149). It does not change today's result — the
    file is genuinely stdlib-only — but without -S the guard tested a SUBSET of the
    condition it exists to prove, which defeats the point of having it.

    Asserts the ENTRY POINT works, not merely that the imports resolve.
    """
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(_MODULE), "--as", "test-cell",
         "--declaration", str(tmp_path / "absent.json")],
        capture_output=True, text=True, timeout=60,
    )
    # 2 (DID NOT MEASURE) is a REPORTING outcome, not a crash: on a box where
    # a followed EnvironmentFile is root-owned, blind coverage must exit 2.
    assert proc.returncode in (0, 1, 2), f"crashed instead of reporting:\n{proc.stderr}"
    assert "cell selfcheck: test-cell" in proc.stdout, proc.stdout
    assert "Traceback" not in proc.stderr, proc.stderr


def test_bare_file_reports_missing_identity_rather_than_guessing(tmp_path):
    """No --as and no $SWARPH_SELF must be exit 2 and a message, never a guess:
    a baseline attributed to the wrong cell is worse than no baseline."""
    env = {"PATH": "/nonexistent"}
    proc = subprocess.run([sys.executable, "-I", "-S", str(_MODULE)],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "pass --as" in proc.stderr, proc.stderr


# ── the per-cell rename is a MIGRATION, and it lands on the baseline ─────────
# droplet, PR #149: with the pre-#148 shared cell_expected.json on disk and the
# per-cell file absent, the run reported DRIFT on three keys and never mentioned
# the file sitting right there. Every cell that declared anything before #148 would
# have produced a baseline full of FALSE DRIFT — the "before" picture #132/#130 is
# diffed against.

_TWO_VALUES = [
    _unit("a.service", "ExecStart=x --state-dir /var/lib/swarph/droplet\n"),
    _unit("b.service", "ExecStart=x --state-dir /var/lib/swarph/droplet-monitor\n"),
]


def test_legacy_declaration_present_is_did_not_measure_not_drift(monkeypatch, tmp_path, capsys):
    """Exit 2, loudly naming both files. NOT exit 1: this run did not measure drift,
    it measured a missing declaration. Empty and blind must not render identically."""
    (tmp_path / "cell_expected.json").write_text(
        json.dumps({"state-dir": ["/var/lib/swarph/droplet",
                                  "/var/lib/swarph/droplet-monitor"]}), encoding="utf-8")
    monkeypatch.setattr(sc, "discover_surfaces", lambda: _TWO_VALUES)
    rc = sc.run_selfcheck(self_name="droplet",
                          declaration=tmp_path / "cell_expected.droplet.json")
    out = capsys.readouterr().out
    assert rc == 2, f"a poisoned baseline is worse than none:\n{out}"
    assert "cell_expected.json" in out, "the legacy file must be NAMED, not silently ignored"
    assert "cell_expected.droplet.json" in out, out
    assert "DRIFT" not in out, "must not report drift it did not measure"


def test_legacy_declaration_is_not_read(monkeypatch, tmp_path, capsys):
    """Auto-reading it would re-introduce the shared one-file-many-cells defect #148
    removed. The migration must be performed, not papered over."""
    (tmp_path / "cell_expected.json").write_text(
        json.dumps({"state-dir": ["/var/lib/swarph/droplet",
                                  "/var/lib/swarph/droplet-monitor"]}), encoding="utf-8")
    monkeypatch.setattr(sc, "discover_surfaces", lambda: _TWO_VALUES)
    sc.run_selfcheck(self_name="droplet", declaration=tmp_path / "cell_expected.droplet.json")
    assert "DECLARED" not in capsys.readouterr().out, "legacy values must not silently apply"


def test_per_cell_declaration_wins_when_both_exist(monkeypatch, tmp_path, capsys):
    """Once migrated, the legacy file's mere presence must not keep firing —
    otherwise the warning becomes noise nobody can clear."""
    (tmp_path / "cell_expected.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cell_expected.droplet.json").write_text(
        json.dumps({"state-dir": ["/var/lib/swarph/droplet",
                                  "/var/lib/swarph/droplet-monitor"]}), encoding="utf-8")
    monkeypatch.setattr(sc, "discover_surfaces", lambda: _TWO_VALUES)
    rc = sc.run_selfcheck(self_name="droplet",
                          declaration=tmp_path / "cell_expected.droplet.json")
    out = capsys.readouterr().out
    assert rc == 0 and "DECLARED" in out, out


def test_no_declaration_anywhere_still_measures(monkeypatch, tmp_path, capsys):
    """The absent-both case is a normal run — drift is a real verdict here."""
    monkeypatch.setattr(sc, "discover_surfaces", lambda: _TWO_VALUES)
    rc = sc.run_selfcheck(self_name="droplet",
                          declaration=tmp_path / "cell_expected.droplet.json")
    assert rc == 1, "two values, nothing declared, no legacy file -> real drift"


# ── coverage: a verdict is only as wide as what was inspected ────────────────
# MEASURED 2026-07-27: grok-researcher's baseline returned `consistent` with 8 flags
# and ZERO crontab rows, while four other cells ON THE SAME BOX saw 18. grok is the
# cell carrying the known relation-broken drift (shape 1) — and that drift lives in
# the crontab it could not read. THE TOOL CERTIFIED CLEAN THE ONE CELL IT WAS
# DESIGNED AROUND, because `crontab -l`'s rc and stderr were both discarded.


def _cov(cls, read, detail=""):
    return {"kind": "coverage", "class": cls, "read": read, "detail": detail}


def test_unreadable_surface_class_is_did_not_measure(monkeypatch, tmp_path, capsys):
    """Exit 2, never `consistent`. A run that could not read a surface class did not
    measure the cell — it measured part of it."""
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("a.service", "ExecStart=x --state-dir /var/lib/swarph/x\n"),
        _cov("crontab", False, "Permission denied"),
    ], "somecell", capsys=capsys)
    assert rc == 2, out
    assert "DID NOT MEASURE" in out and "crontab" in out


def test_legitimately_absent_crontab_is_read_and_still_reported(monkeypatch, tmp_path, capsys):
    """`no crontab for <user>` is a real answer, NOT a failure — but it must still be
    stated, because this cell's cron line may live under ANOTHER user where this cell
    cannot see it. That is exactly grok-researcher's situation."""
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("a.service", "ExecStart=x --state-dir /var/lib/swarph/x\n"),
        _cov("crontab", True, "no crontab for this user — a cron line for this cell "
                              "may exist under another user"),
    ], "grok-researcher", capsys=capsys)
    assert rc == 0, out
    assert "COVERAGE" in out and "another user" in out


def test_verdict_line_states_what_it_is_a_verdict_about(monkeypatch, tmp_path, capsys):
    """Bare `consistent` was read as 'this cell is correctly configured'.

    drop-on-meta-edge, reviewing PR #243, held that misreading for several
    seconds after reading the card, the DM, and the diff. The COVERAGE block
    eventually corrected it; the verdict line itself must carry the property
    so a reader who stops at the last line is not certified into a lie.
    """
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("a.service", "ExecStart=x --as lab --state-dir /var/lib/swarph/lab\n"),
    ], "lab", capsys=capsys)
    assert rc == 0, out
    assert "verdict: consistent" in out
    assert "surfaces agree with each other" in out
    # GAP 1 is held now: the scope line must say the resolver comparison
    # happened, not that it did not.
    assert "AND with resolver output" in out
    assert "NOT compared against resolver output" not in out


def test_coverage_is_printed_even_on_a_clean_run(monkeypatch, tmp_path, capsys):
    """A block that appears only on failure is one nobody reads until too late."""
    _, out = _run(monkeypatch, tmp_path, [_cov("crontab", True, "51 lines")],
                  "lab-ovh", capsys=capsys)
    assert "COVERAGE  crontab" in out and "51 lines" in out


def test_not_inspected_classes_do_not_block_the_verdict(monkeypatch, tmp_path, capsys):
    """`running processes` is NOT INSPECTED BY DESIGN — declared config only. It must
    be DISCLOSED (science-claude's live monitor is hand-started and invisible here, so
    a migration could install a second one) but it must not turn every run into
    DID NOT MEASURE, or the signal becomes noise and gets ignored."""
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("a.service", "ExecStart=x --state-dir /var/lib/swarph/x\n"),
        _cov("running processes", False, "NOT INSPECTED BY DESIGN"),
    ], "science-claude", capsys=capsys)
    assert rc == 0, out
    assert "running processes" in out and "NOT INSPECTED" in out


@pytest.mark.parametrize("rc,out,err", [
    (0, "*/5 * * * * swarph watchdog --cell x\n", ""),   # has a crontab
    (0, "", ""),                                          # present but empty
    (1, "", "no crontab for grok"),                       # grok's case
])
def test_other_user_caveat_is_on_every_readable_branch(rc, out, err):
    """It is a property of THE PROBE — this command cannot read another user's crontab
    in ANY branch — not of the cell. Hanging it off the no-crontab branch only would
    tell grok and stay silent for a cell that HAS a crontab AND a line under another
    user (droplet, PR #150)."""
    cov = [s for s in sc.user_crontab_surfaces(rc, out, err) if s.get("kind") == "coverage"]
    assert cov and cov[0]["read"] is True
    assert "other users" in cov[0]["detail"], cov


def test_user_crontab_class_is_named_for_what_it_actually_reads():
    """`crontab -l` reads THE INVOKING USER'S crontab and nothing else.

    A class named `crontab` overstated its scope BY THE NAME ALONE, before any logic
    ran: a cell with a swarph line in /etc/cron.d would read "COVERAGE crontab read /
    verdict: consistent" while a live cron surface was never opened — grok's failure
    moved one directory over (droplet, PR #150).
    """
    cls = {s["class"] for s in sc.user_crontab_surfaces(0, "x\n", "")
           if s.get("kind") == "coverage"}
    assert cls == {"user crontab"}, f"class name overstates its scope: {cls}"


def test_absent_cron_platform_is_not_the_same_as_failed_to_read(monkeypatch):
    """NOT APPLICABLE is a THIRD state, and Windows CI is what found it.

    On Windows there is no crontab binary, so the probe raised, the class went
    read=False, and the whole run returned DID NOT MEASURE. But "this platform has no
    cron" is not "I could not read the cron" — the surface does not exist to be read.
    Collapsing them makes every Windows cell permanently unmeasurable, which teaches
    people to ignore exit 2 on the platform where it will one day mean something.

    A PermissionError must still be blind: we meant to read it and could not.
    """
    def boom(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory: 'crontab'")

    monkeypatch.setattr(sc.subprocess, "run", boom)
    cov = [s for s in sc.discover_surfaces()
           if s.get("kind") == "coverage" and s.get("class") == "user crontab"]
    assert cov and cov[0]["read"] is True, cov
    assert "not applicable" in cov[0]["detail"], cov

    def denied(cmd, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sc.subprocess, "run", denied)
    cov = [s for s in sc.discover_surfaces()
           if s.get("kind") == "coverage" and s.get("class") == "user crontab"]
    assert cov and cov[0]["read"] is False, "a real failure must stay blind"


def test_system_cron_absent_platform_is_not_applicable():
    """`none present` reads as I LOOKED AND THERE WERE NONE. On a box with no cron at
    all that is the wrong answer, and it is the one a reader would trust — it sat
    beside `user crontab: not applicable` describing ONE platform fact two ways.

    This class fails by an EMPTY LOOP rather than an exception, so the
    FileNotFoundError fix for its sibling could not reach it (droplet, read from the
    branch and flagged READ-NOT-MEASURED; measured here).
    """
    c = sc.system_cron_coverage([], [], [], platform_has_cron=False)
    assert c["read"] is True and "not applicable" in c["detail"], c


def test_system_cron_present_but_empty_is_distinct_from_absent():
    """A cron directory that exists and holds nothing IS a measurement."""
    c = sc.system_cron_coverage([], [], [], platform_has_cron=True)
    assert c["read"] is True and c["detail"] == "present but empty", c


def test_system_cron_unreadable_still_wins_over_platform_absence():
    """Unreadable is blind and must stay exit-2, whatever the platform looks like."""
    c = sc.system_cron_coverage([], [], ["cron.d/x (PermissionError)"],
                                platform_has_cron=False)
    assert c["read"] is False, c


def test_system_cron_counts_stay_hand_reconcilable():
    c = sc.system_cron_coverage(["crontab", "certbot", "sysstat"], [".placeholder"], [],
                                platform_has_cron=True)
    assert c["detail"].startswith("3 live, 1 ignored"), c


def test_a_real_probe_failure_is_not_read(monkeypatch):
    """A locale-translated message, a permission error, a missing binary — anything
    that is not a recognised empty must degrade to NOT READ, never to falsely-clean."""
    cov = sc.user_crontab_surfaces(1, "", "crontab: permiso denegado")[0]
    assert cov["read"] is False and "denegado" in cov["detail"]


@pytest.mark.parametrize("name,runs", [
    ("certbot", True), ("e2scrub_all", True), ("swarph-monitor", True), ("my_job", True),
    (".placeholder", False), ("swarph.conf", False), ("foo.bak", False),
    ("x.dpkg-dist", False),
])
def test_cron_d_filename_rule(name, runs):
    """cron runs only ^[A-Za-z0-9_-]+$ in /etc/cron.d — ANY DOT disqualifies the file.

    A swarph line in a dot-named file would otherwise be read as live configuration
    and could produce DRIFT / UNOWNED / RELATION BROKEN for a line CRON WILL NEVER RUN
    (droplet, PR #150). That is the FOSSIL distinction the systemd path already models,
    expressed here as a filename rule instead of unit state.
    """
    assert bool(sc._CRON_D_NAME_RE.match(name)) is runs


def test_inert_cron_file_is_fossil_not_drift(monkeypatch, tmp_path, capsys):
    """A dot-named cron file's contents must be REPORTED but never counted as drift —
    same contract as an inactive+disabled unit."""
    rc, out = _run(monkeypatch, tmp_path, [
        {"name": "(/etc/cron.d/swarph.conf)", "kind": "cron", "shared": True, "live": False,
         "text": "*/5 * * * * root swarph watchdog --cell lab --cursor /nope/cursor.json\n"},
        _unit("a.service", "ExecStart=x --as lab --state-dir /var/lib/swarph/lab\n"),
    ], "lab", capsys=capsys)
    assert "FOSSIL" in out, out
    assert rc == 0, "cron will never run it — it cannot be drift"


def test_coverage_entries_are_not_parsed_as_flags(monkeypatch, tmp_path, capsys):
    """Coverage rides the same injectable channel as surfaces, so it must not leak
    into the flag count or invent rows."""
    assert sc.extract(_cov("crontab", True, "--state-dir /nope")) == []


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


# ── 7. cursor-lin: instance drop-in is where the live ExecStart lives ────────
# MEASURED 2026-08-17 on cursor-lin (this box). `swarph-monitor@.service` carries
# `--as %i` (shape 6, skipped as placeholder). The live values
# (`--as cursor-lin --token-file … --gateway …`) live ONLY in
# `swarph-monitor@cursor-lin.service.d/override.conf`.
# The old glob `*swarph*.service` never opened a `.d/` directory, so this cell's
# own monitor certified `consistent` with ZERO owned flags — the same lie as
# grok-researcher's clean verdict over an unread crontab.


def test_dropin_parent_unit_is_the_instance_not_the_template():
    assert (
        sc.dropin_parent_unit("swarph-monitor@cursor-lin.service.d/override.conf")
        == "swarph-monitor@cursor-lin.service"
    )
    assert sc.dropin_parent_unit("swarph-monitor@.service") is None
    assert sc.dropin_parent_unit("swarph-monitor.service.d/override.conf") == (
        "swarph-monitor.service"
    )


def test_unit_discovery_reads_instance_dropins_and_content_relevance(tmp_path):
    """Two measured blindnesses, one test each half:

    1. A glob matching only `*.service` is blind to the drop-in file systemd
       actually runs (the SEVENTH SHAPE — cursor-lin's live values exist only
       in swarph-monitor@cursor-lin.service.d/override.conf).
    2. A glob matching only *swarph* NAMES is blind to the unit that was dead
       76 days on lab-ovh: refresh-features-snapshot.service is relevant by
       CONTENT, not name.
    """
    (tmp_path / "swarph-monitor@.service").write_text(
        "ExecStart=swarph monitor start --as %i\n", encoding="utf-8"
    )
    drop = tmp_path / "swarph-monitor@cursor-lin.service.d"
    drop.mkdir()
    (drop / "override.conf").write_text(
        "ExecStart=swarph monitor start --as cursor-lin --token-file /t\n",
        encoding="utf-8",
    )
    candidates = [p.relative_to(tmp_path).as_posix()
                  for p in sorted(tmp_path.glob("*.service"))
                  + sorted(tmp_path.glob("*.service.d/*.conf"))]
    assert "swarph-monitor@.service" in candidates
    assert "swarph-monitor@cursor-lin.service.d/override.conf" in candidates

    assert sc.unit_is_relevant("mdmonitor.service", "not ours\n") is False
    assert sc.unit_is_relevant(
        "refresh-features-snapshot.service",
        "Description=swarph feature-registry — refresh metaedge snapshot\n",
    ) is True


def test_instance_dropin_text_is_owned_by_the_named_cell():
    rows = sc.extract(_unit(
        "swarph-monitor@cursor-lin.service.d/override.conf",
        "ExecStart=swarph monitor start --as cursor-lin "
        "--gateway http://100.107.222.72:8788 "
        "--token-file /home/ubuntu/.config/swarph/cursor-lin.peer_token\n",
    ))
    by_key = {r.key: r.value for r in rows}
    assert by_key["as"] == "cursor-lin"
    assert by_key["gateway"] == "http://100.107.222.72:8788"
    assert by_key["token-file"].endswith("cursor-lin.peer_token")
    assert all(r.owner == "cursor-lin" for r in rows)


# ── Resolver comparison (GAP 1) — the two 76-days-dead specimens ────────────
# Both from lab-ovh, 2026-08-18: refresh-features-snapshot died 2026-06-03 and
# its watchdog alerted into a void ~2000x/week. Specimen 1: wrong host.
# Specimen 2: wrong-era credential, hidden behind specimen 1.


def _envfile(text, owner="lab-ovh", live=True, name="/etc/default/x (via x.service)"):
    return {"name": name, "kind": "envfile", "text": text, "live": live,
            "owner": owner}


def _resolver(gateway="http://100.107.222.72:8788", token=("t" * 43, "peer file"),
              listeners=frozenset({("100.107.222.72", 8788)})):
    return {"gateway": gateway, "token": token, "listeners": set(listeners),
            "socket_table": "read"}


def test_specimen1_wrong_host_env_literal_is_resolver_drift(monkeypatch, tmp_path, capsys):
    """MESH_GATEWAY_URL=localhost:8788 while the resolver says tailnet."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://localhost:8788\n"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert rc == 1
    assert "RESOLVER DRIFT" in out
    assert "localhost:8788" in out
    assert "100.107.222.72:8788" in out


def test_specimen1_also_caught_by_listener_when_literal_matches_default(monkeypatch, tmp_path, capsys):
    """The subtle half: the dead unit's literal EQUALED the resolver default
    (both localhost). Literal-vs-resolver alone reports agreement — the
    socket table is what catches 'nothing listens there'."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://localhost:8788\n"),
    ], "lab-ovh", capsys=capsys,
        resolver=_resolver(gateway="http://localhost:8788"))
    assert rc == 1
    assert "RESOLVER DRIFT" not in out  # literal == resolver: agreement
    assert "UNREACHABLE" in out and "localhost:8788" in out


def test_absent_socket_table_is_did_not_measure_not_false_clean(monkeypatch, tmp_path, capsys):
    """macOS has no /proc (this repo's macOS leg exists because a /proc
    dependency shipped undetected once already, card #492). A run that never
    saw a socket table must say DID NOT MEASURE — not a silent skip, not a
    false clean, and NOT UNREACHABLE everywhere (the false-calm lie inverted
    is false alarm)."""
    resolver = {"gateway": "http://100.107.222.72:8788",
                "token": (None, "no token resolvable"),
                "listeners": set(), "socket_table": "absent"}
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://100.107.222.72:8788\n"),
    ], "lab-ovh", capsys=capsys, resolver=resolver)
    assert rc == 2
    assert "DID NOT MEASURE" in out and "socket table" in out
    assert "UNREACHABLE" not in out
    assert "no /proc on this platform" in out


def test_unreadable_socket_table_is_also_blind(monkeypatch, tmp_path, capsys):
    """Non-vacuity partner: a PARTIAL read (one /proc file failed) is blind
    too — half a socket table is not a measurement."""
    resolver = {"gateway": None, "token": (None, "no token resolvable"),
                "listeners": {("127.0.0.1", 22)}, "socket_table": "unreadable"}
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("a.service", "ExecStart=x --as lab-ovh --state-dir /s\n"),
    ], "lab-ovh", capsys=capsys, resolver=resolver)
    assert rc == 2
    assert "DID NOT MEASURE" in out and "unreadable" in out


def test_remote_gateway_is_not_marked_unreachable(monkeypatch, tmp_path, capsys):
    """A cell whose gateway is on ANOTHER box must not report UNREACHABLE
    for a listener that was never supposed to be local."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://203.0.113.9:8788\n"),
    ], "lab-ovh", capsys=capsys,
        resolver=_resolver(gateway="http://203.0.113.9:8788"))
    assert rc == 0, out
    assert "UNREACHABLE" not in out


def test_specimen2_wrong_era_token_is_mismatch_and_redacted(monkeypatch, tmp_path, capsys):
    """64-char pre-migration literal vs 43-char peer token. The LENGTH PAIR
    is the finding; neither value may appear in the output."""
    old_token = "ab" * 32  # 64 chars, the pre-#311 shape
    new_token = "cd" * 21 + "e"  # 43 chars
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile(f"MESH_GATEWAY_TOKEN={old_token}\n"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver(token=(new_token, "peer file")))
    assert rc == 1
    assert "TOKEN MISMATCH" in out
    assert "64 chars" in out and "43 chars" in out
    assert old_token not in out and new_token not in out  # redaction is the test


def test_token_match_is_ok_and_still_redacted(monkeypatch, tmp_path, capsys):
    """Non-vacuity partner: a MATCHING literal must not mismatch."""
    tok = "z" * 43
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile(f"MESH_GATEWAY_TOKEN={tok}\n"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver(token=(tok, "peer file")))
    assert rc == 0, out
    assert "TOKEN MISMATCH" not in out
    assert "matches resolved credential" in out
    assert tok not in out


def test_unparseable_gateway_is_malformed_not_silent(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, [
        _unit("a.service", "ExecStart=x --as lab-ovh --gateway =\n"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert "MALFORMED" in out  # empty value, caught by the existing shape


def test_no_resolver_claim_bucket_is_counted(monkeypatch, tmp_path, capsys):
    """The bucket's SIZE is reported so it cannot quietly grow into the
    place findings go to die (lab-ovh, DM 24706). Bucket members are
    env-file mesh keys the resolver does not own."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://100.107.222.72:8788\n"
                 "MESH_OUTBOX_DIR=/var/spool/mesh\n"
                 "SWARPH_LEGACY_THING=1\n"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert "NO-RESOLVER-CLAIM" in out
    assert "2 literal(s)" in out
    assert "env:MESH_OUTBOX_DIR" in out and "env:SWARPH_LEGACY_THING" in out
    # the resolver-owned key is NOT in the bucket
    assert "env:MESH_GATEWAY_URL" not in out.split("NO-RESOLVER-CLAIM")[1]


def test_bucket_empty_when_only_owned_keys(monkeypatch, tmp_path, capsys):
    """Non-vacuity partner: owned keys alone must produce an empty bucket."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://100.107.222.72:8788\n"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert "NO-RESOLVER-CLAIM  0 literal(s)" in out


def test_declared_gateway_divergence_is_not_drift(monkeypatch, tmp_path, capsys):
    """Intentional divergence stays declarable at the resolver layer too."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_URL=http://localhost:8788\n"),
    ], "lab-ovh", expected={"gateway": ["http://localhost:8788"]},
        capsys=capsys, resolver=_resolver())
    assert "DECLARED" in out
    assert "RESOLVER DRIFT" not in out


def test_envfile_extraction_ignores_unowned_keys_and_placeholders():
    rows = sc.extract_envfile(_envfile(
        "MESH_GATEWAY_URL=http://x:1\n"
        "RANDOM_LOCAL_THING=42\n"
        "MESH_GATEWAY_TOKEN=${TOKEN_FROM_ELSEWHERE}\n"
        "# comment\n"
    ))
    assert [r.key for r in rows] == ["env:MESH_GATEWAY_URL"]
    assert rows[0].secret is False


def test_envfile_token_row_is_marked_secret():
    rows = sc.extract_envfile(_envfile("MESH_GATEWAY_TOKEN=abc123\n"))
    assert rows[0].secret is True
    assert sc.display_value(rows[0]) == "<redacted, 6 chars>"


def test_parse_listen_sockets_reads_proc_net_tcp_shape():
    # 0100007F:2254 = 127.0.0.1:8788 LISTEN; the tailnet specimen row
    # 48DE6B64:2254 = 100.107.222.72:8788 LISTEN (little-endian hex).
    text = (
        "  sl  local_address rem_address   st ...\n"
        "   0: 48DE6B64:2254 00000000:0000 0A ...\n"
        "   1: 0100007F:8A2E 00000000:0000 0A ...\n"
        "   2: 0100007F:2254 00000000:0000 05 ...\n"  # not LISTEN
    )
    listeners = sc.parse_listen_sockets([text])
    assert ("100.107.222.72", 8788) in listeners
    assert ("127.0.0.1", 35374) in listeners
    assert ("127.0.0.1", 8788) not in listeners  # st != 0A


def test_gateway_reachability_classes():
    listeners = {("100.107.222.72", 8788), ("127.0.0.1", 22)}
    assert sc.gateway_reachability("http://100.107.222.72:8788", listeners) == "local-listening"
    assert sc.gateway_reachability("http://localhost:8788", listeners) == "local-silent"
    assert sc.gateway_reachability("http://127.0.0.1:22", listeners) == "local-listening"
    assert sc.gateway_reachability("http://203.0.113.9:8788", listeners) == "remote-unchecked"
    assert sc.gateway_reachability("not a url at all :::", listeners) in (
        "unparseable", "remote-unchecked")  # never local-silent on garbage


def test_gateway_own_token_is_declarable_by_surface_and_length(monkeypatch, tmp_path, capsys):
    """The gateway's OWN env file carries a server-side token that should
    never equal a peer token (measured: mesh-gateway.service on lab-ovh).
    A permanent red line there trains ignoring the signal — so it is
    declarable, by surface + redacted length, with no secret in the file."""
    surface = "/home/ubuntu/mesh-gateway/.env (via mesh-gateway.service)"
    server_tok = "s" * 43
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile(f"MESH_GATEWAY_TOKEN={server_tok}\n", name=surface, owner=None),
    ], "cursor-lin",
        expected={f"env:MESH_GATEWAY_TOKEN@{surface}": ["<redacted, 43 chars>"]},
        capsys=capsys, resolver=_resolver(token=("p" * 43, "peer file")))
    assert rc == 0, out
    assert "DECLARED" in out
    assert "TOKEN MISMATCH" not in out
    assert server_tok not in out


def test_undeclared_surface_token_mismatch_still_fires(monkeypatch, tmp_path, capsys):
    """Non-vacuity partner for the declaration path: a DIFFERENT surface
    with the same-length token is not covered by the declaration."""
    surface = "/home/ubuntu/mesh-gateway/.env (via mesh-gateway.service)"
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile(f"MESH_GATEWAY_TOKEN={'s' * 43}\n", name=surface, owner=None),
        _envfile(f"MESH_GATEWAY_TOKEN={'x' * 43}\n", owner=None,
                 name="/etc/default/refresh-features-snapshot (via refresh-features-snapshot.service)"),
    ], "cursor-lin",
        expected={f"env:MESH_GATEWAY_TOKEN@{surface}": ["<redacted, 43 chars>"]},
        capsys=capsys, resolver=_resolver(token=("p" * 43, "peer file")))
    assert rc == 1
    assert out.count("TOKEN MISMATCH") == 1  # only the undeclared surface
    assert "refresh-features-snapshot" in out.split("TOKEN MISMATCH")[1]


def test_empty_env_value_is_declarable_by_surface(monkeypatch, tmp_path, capsys):
    """PR #261 review: an EMPTY env value is ambiguous — 'deliberately
    disabled' and 'never filled in' look identical (measured: the gateway's
    empty MESH_GATEWAY_COMMANDER_TOKEN is vestigial and guarded, not a
    hole). The declaration is how the operator says which."""
    surface = "/home/ubuntu/mesh-gateway/.env (via mesh-gateway.service)"
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_COMMANDER_TOKEN=\n", name=surface, owner=None),
    ], "cursor-lin",
        expected={f"env:MESH_GATEWAY_COMMANDER_TOKEN@{surface}": ["<EMPTY>"]},
        capsys=capsys, resolver=_resolver())
    assert rc == 0, out
    assert "DECLARED" in out and "MALFORMED" not in out


def test_undeclared_empty_env_value_still_malformed(monkeypatch, tmp_path, capsys):
    """Non-vacuity partner: without the declaration, empty is MALFORMED."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile("MESH_GATEWAY_COMMANDER_TOKEN=\n", owner=None),
    ], "cursor-lin", capsys=capsys, resolver=_resolver())
    assert rc == 1
    assert "MALFORMED" in out


# ── PR #261 Copilot findings (confirmed by lab-ovh, DM 24824) ───────────────


def test_quoted_env_value_matches_unquoted_resolver(monkeypatch, tmp_path, capsys):
    """Finding 1: KEY="value" kept its quotes and could never match the
    resolver's unquoted output — a false-positive generator in a drift
    detector."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile('MESH_GATEWAY_URL="http://100.107.222.72:8788"\n'),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert rc == 0, out
    assert "RESOLVER DRIFT" not in out


def test_quoted_wrong_value_still_drifts(monkeypatch, tmp_path, capsys):
    """Non-vacuity partner: quote-stripping must not launder a genuinely
    wrong quoted value into agreement."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile('MESH_GATEWAY_URL="http://10.9.9.9:8788"\n'),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert rc == 1
    assert "RESOLVER DRIFT" in out and "10.9.9.9" in out


def test_multi_file_environment_file_directive_all_read():
    """Finding 2: systemd permits multiple files on one directive. Taking
    the RHS as one path makes every additional file silently UNREAD —
    under-measurement reported as coverage, the worse failure mode."""
    refs = sc.environment_file_refs(
        "[Service]\n"
        "EnvironmentFile=-/etc/default/a /etc/default/b\n"
        'EnvironmentFile="-/etc/default/c with space"\n'
    )
    assert refs == ["/etc/default/a", "/etc/default/b", "/etc/default/c with space"]


def test_single_file_directive_unchanged():
    """Non-vacuity partner: the common single-file form still parses,
    optional-dash stripped."""
    assert sc.environment_file_refs("EnvironmentFile=-/etc/default/x\n") == ["/etc/default/x"]


def test_wildcard_bind_address_is_not_a_target():
    """Finding 3: 0.0.0.0/:: are wildcard BIND addresses. A unit pointing at
    one is a misconfiguration to catch, not a loopback to bless — even when
    a wildcard listener exists on the port."""
    listeners = {("0.0.0.0", 8788)}
    assert sc.gateway_reachability("http://0.0.0.0:8788", listeners) == "wildcard-target"
    assert sc.gateway_reachability("http://[::]:8788", listeners) == "wildcard-target"


def test_wildcard_listener_still_serves_loopback_target():
    """Non-vacuity partner: a listener ON the wildcard address does serve a
    loopback target — the listener side keeps the address valid."""
    assert sc.gateway_reachability("http://localhost:8788", {("0.0.0.0", 8788)}) == "local-listening"


def test_same_length_secrets_display_as_distinct_numbered_values(monkeypatch, tmp_path, capsys):
    """Finding 4: two DIFFERENT 43-char tokens displayed as one repeated
    string while reporting '2 values' — and two 43-char tokens is exactly
    the peer-token case. Numbered redaction keeps the count visible."""
    rc, out = _run(monkeypatch, tmp_path, [
        _envfile(f"MESH_GATEWAY_TOKEN={'a' * 43}\n", name="/e/a (via a.service)"),
        _envfile(f"MESH_GATEWAY_TOKEN={'b' * 43}\n", name="/e/b (via b.service)"),
    ], "lab-ovh", capsys=capsys, resolver=_resolver())
    assert "DRIFT" in out
    assert "<redacted #1, 43 chars>" in out and "<redacted #2, 43 chars>" in out
    assert "['<redacted, 43 chars>', '<redacted, 43 chars>']" not in out
    assert "a" * 43 not in out and "b" * 43 not in out
