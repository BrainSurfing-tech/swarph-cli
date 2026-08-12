"""`resolve_session_pane` must return a FULLY-QUALIFIED target, never a pane-id.

>>> IT RETURNED `#{pane_id}` (%N) UNTIL 2026-08-12, AND ON PSMUX THAT IS AMBIGUOUS. <<<
On real tmux %N is unique per SERVER — the entire purpose of id targeting. psmux
allocates ids PER SESSION, so `list-panes -a` reports two co-resident sessions as
paneid=%1 winid=@1. The `list-panes -t <self_name>` call is correctly scoped; the id it
RETURNED was then used UNSCOPED by capture-pane and send-keys. Result on a multi-session
Windows box: probe_pane could read ANOTHER CELL'S SCREEN, and inject() could deliver a
DM INTO ANOTHER CELL'S PANE. Silently, exit 0.

AND IT IS NOT DETERMINISTIC BY SORT ORDER — gpu-wsl first reported "the resolver picks
whichever session sorts first" and then refuted it with their own data: `-t %1` resolves
to whatever the CURRENT ROUTING DEFAULT is. One target, two answers, depending on
ambient context. An INTERMITTENT cross-cell misdelivery is worse than a stable one,
because it reproduces only under a condition nobody records.

A BARE SESSION NAME WOULD NOT DO — `send-keys -t <session>` lands on the ACTIVE pane,
which on a multi-pane cell can be a SHELL where an injected "/model ..." runs as a shell
command. The positive claude/node identification is the property being preserved; only
the target FORM changes.

Measurement that authorised the change (gpu-wsl, `psmux display-message -p -t`, a
side-effect-free resolution query): with the routing default PINNED AT THE NEIGHBOURING
SESSION, the fully-qualified form still resolved to its own session, 4/4.
"""
import swarph_cli.session_bridge as sb


class _CP:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def _mux_out(monkeypatch, out, sink=None):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")

    def _run(cmd, **kw):
        if sink is not None:
            sink.append(cmd)
        return _CP(0, out)

    monkeypatch.setattr(sb.subprocess, "run", _run)


def test_resolve_returns_a_SESSION_QUALIFIED_target_not_a_pane_id(monkeypatch):
    """>>> THE HEADLINE. <<< `lab-ovh:0.1`, never `%1` — the returned string is used
    UNSCOPED downstream, so it must carry its own session."""
    _mux_out(monkeypatch, "0 0 bash\n0 1 claude\n")
    assert sb.resolve_session_pane("lab-ovh") == "lab-ovh:0.1"


def test_the_returned_target_ALWAYS_CARRIES_THE_CELLS_OWN_SESSION_NAME(monkeypatch):
    """The property that actually closes the bug, asserted directly rather than
    inferred from the happy-path string above: whatever indices the multiplexer
    reports, the target is prefixed with THIS cell's session. A regression to a bare
    id or a bare index would pass a string-equality test written for one fixture and
    fail this one."""
    for name, out, want in (
        ("gpt-lc", "0 0 claude\n", "gpt-lc:0.0"),
        ("workstation-lc", "3 7 node\n", "workstation-lc:3.7"),
        ("cell", "0 0 bash\n2 5 claude\n", "cell:2.5"),
    ):
        _mux_out(monkeypatch, out)
        got = sb.resolve_session_pane(name)
        assert got == want
        assert got.startswith(f"{name}:"), f"{got} does not name its own session"


def test_it_ASKS_the_multiplexer_for_indices_not_for_pane_id(monkeypatch):
    """>>> THE CONTROL THAT MAKES THE REST MEAN SOMETHING. <<< An implementation that
    kept requesting `#{pane_id}` and then string-built a session prefix around it
    would satisfy every assertion above while still resolving through an ambiguous
    id if anything downstream ever split on it. Pin the FORMAT STRING actually sent."""
    seen = []
    _mux_out(monkeypatch, "0 1 claude\n", sink=seen)
    sb.resolve_session_pane("cell")
    fmt = " ".join(seen[0])
    assert "#{window_index}" in fmt and "#{pane_index}" in fmt
    assert "#{pane_id}" not in fmt, "still requesting the ambiguous per-session id"


def test_resolve_returns_node_pane(monkeypatch):
    _mux_out(monkeypatch, "1 2 node\n")
    assert sb.resolve_session_pane("cell") == "cell:1.2"


def test_resolve_none_when_only_shell_panes(monkeypatch):
    """The positive-identification property, unchanged by this fix and the reason a
    bare session name is not an acceptable target: an injected prompt must never
    reach a shell."""
    _mux_out(monkeypatch, "0 0 bash\n0 1 vim\n")
    assert sb.resolve_session_pane("cell") is None


def test_resolve_none_on_nonzero(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: "tmux")
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _CP(1, ""))
    assert sb.resolve_session_pane("cell") is None


def test_resolve_none_when_no_mux(monkeypatch):
    monkeypatch.setattr(sb, "_mux", lambda: None)
    assert sb.resolve_session_pane("cell") is None


def test_a_SHORT_line_is_ignored_rather_than_indexed_into(monkeypatch):
    """Three fields are now required where two were before. A malformed or truncated
    line must be skipped, not IndexError — the resolver's whole contract is that it
    returns None on any failure so the caller stays surface-only and NEVER injects."""
    _mux_out(monkeypatch, "0 claude\nbroken\n0 1 claude\n")
    assert sb.resolve_session_pane("cell") == "cell:0.1"
