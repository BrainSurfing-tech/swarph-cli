import pytest

import swarph_cli.stall_alert as st


@pytest.fixture(autouse=True)
def _isolated_cells_dir(tmp_path, monkeypatch):
    """The stall alert reads THIS cell's own yaml (`responsible:`). Never let a test
    read the real ~/.config/swarph/cells of the machine it runs on."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    d = tmp_path / "config" / "swarph" / "cells"
    d.mkdir(parents=True)
    return d


def _cell_yaml(cells_dir, name, text):
    (cells_dir / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_is_alert_tick_backoff_sequence():
    fire = [n for n in range(1, 500) if st.is_alert_tick(n)]
    assert fire[:4] == [60, 120, 240, 480]


def test_is_alert_tick_below_threshold():
    assert not any(st.is_alert_tick(n) for n in range(0, 6))


def test_first_alert_is_60_after_the_measured_drains():
    """7 of 7 stall rows on 2026-09-30/10-01 drained in 7-25 min. 6 ticks
    is 3 min and must not fire. Fails on main, where 6 is the first tick."""
    for n in (6, 12, 24, 48):
        assert st.is_alert_tick(n) is False
    assert st.is_alert_tick(60) is True
    assert st.is_alert_tick(120) is True
    assert st.is_alert_tick(240) is True
    assert st.is_alert_tick(180) is False


class _Board:
    """One card's obligations. A second ask while one is open is the defect."""

    def __init__(self, owners=None, allow_messages=False, peers_status=200):
        self.rows = []
        self.calls = []
        self.next_id = 1
        self.owners = owners or {}
        self.allow_messages = allow_messages
        self.peers_status = peers_status
        self.messages = []

    def handle(self, req, timeout=None):
        import json
        url = req.full_url
        self.calls.append((req.get_method(), url, req.data))
        class _Resp:
            def __init__(self, raw):
                self.status = 200
                self._raw = raw
            def read(self):
                return self._raw
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        if "/messages" in url:
            if not self.allow_messages:
                raise AssertionError(f"main DMs commander: {url}")
            self.messages.append(json.loads(req.data.decode()))
            return _Resp(b'{"id": 1}')

        if req.get_method() == "GET" and "/peers/" in url:
            if self.peers_status != 200:
                raise OSError("peer registry down")
            name = url.rsplit("/", 1)[-1]
            row = {"name": name, "enabled": True}
            if name in self.owners:       # today the gateway returns no owner key at all
                row["owner"] = self.owners[name]
            return _Resp(json.dumps(row).encode())
        if req.get_method() == "GET" and "obligations?" in url:
            want = "open"
            if "status=fallback_fired" in url:
                want = "fallback_fired"
            live = [r for r in self.rows if r["status"] == want]
            return _Resp(json.dumps({"obligations": live}).encode())
        if req.get_method() == "POST" and url.endswith("/ask"):
            body = json.loads(req.data.decode())
            hours = body.get("timeout_hours")
            row = {"id": self.next_id, "status": "open",
                   "holder": body["holder"], "what": body["what"],
                   "accept": body["accept"],
                   "timeout_at": "2026-09-30T14:00:00+00:00" if hours else None,
                   "fallback_target": body.get("fallback_target")}
            self.next_id += 1
            self.rows.append(row)
            return _Resp(json.dumps({"id": row["id"]}).encode())
        if req.get_method() == "POST" and "/close" in url:
            oid = int(url.rstrip("/").split("/")[-2])
            for row in self.rows:
                if row["id"] == oid:
                    row["status"] = "closed"
            return _Resp(b"{}")
        raise AssertionError(url)


def test_stall_opens_one_obligation_held_by_lab_ovh(monkeypatch):
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "workstation-lc", 12, 3) is True
    assert st.send_stall_alert("http://gw", "tok", "workstation-lc", 24, 3) is True
    asks = [c for c in board.calls if c[0] == "POST" and c[1].endswith("/ask")]
    assert len(asks) == 1
    import json
    body = json.loads(asks[0][2].decode())
    assert body["holder"] == "lab-ovh"
    assert body["created_by"] == "workstation-lc"
    assert "workstation-lc" in body["what"]
    assert "ticks=12" in body["what"]
    assert "pending_dms=3" in body["what"]
    assert "commander" not in json.dumps(body)
    assert body["fallback_target"] != body["created_by"]
    assert body["timeout_hours"] == st._TIMEOUT_HOURS == 2
    assert board.rows[0]["timeout_at"] is not None
    assert all("/messages" not in c[1] for c in board.calls)


def test_drain_closes_the_stall_row(monkeypatch):
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell", 6, 2) is True
    assert hasattr(st, "clear_stall_alert"), (
        "opens no per-cell row: main DMs commander and cannot close one cell")
    assert st.clear_stall_alert("http://gw", "tok", "cell") is True
    assert all(r["status"] == "closed" for r in board.rows)
    assert st.clear_stall_alert("http://gw", "tok", "cell") is True
    closes = [c for c in board.calls if "/close" in c[1]]
    assert len(closes) == 1


def test_two_cells_do_not_share_a_row(monkeypatch):
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-a", 6, 1) is True
    assert st.send_stall_alert("http://gw", "tok", "cell-b", 6, 4) is True
    asks = [c for c in board.calls if c[0] == "POST" and c[1].endswith("/ask")]
    assert len(asks) == 2
    import json
    named = [json.loads(c[2].decode())["what"] for c in asks]
    assert any("cell=cell-b " in text for text in named)
    assert hasattr(st, "clear_stall_alert"), (
        "opens no per-cell row: main DMs commander and cannot close one cell")
    assert st.clear_stall_alert("http://gw", "tok", "cell-b") is True
    still = [r for r in board.rows if r["status"] == "open"]
    closed = [r for r in board.rows if r["status"] == "closed"]
    assert len(still) == 1 and "cell=cell-a " in still[0]["what"]
    assert len(closed) == 1 and "cell=cell-b " in closed[0]["what"]


def test_lab_ovh_stall_is_held_by_someone_else(monkeypatch):
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "lab-ovh", 6, 1) is True
    import json
    body = json.loads(board.calls[-1][2].decode())
    assert body["holder"] != "lab-ovh"
    assert body["fallback_target"] != "lab-ovh"
    assert body["fallback_target"] == body["holder"]
    assert board.rows[0]["timeout_at"] is not None


def test_send_stall_alert_failsafe_on_error(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(st.urllib.request, "urlopen", boom)
    assert st.send_stall_alert("http://gw", "tok", "cell", 6, 1) is False


def test_send_stall_alert_failsafe_on_bad_gateway():
    # a schemeless / misconfigured gateway must be caught at Request-build time,
    # not raised (no monkeypatch — Request(...) itself raises ValueError).
    assert st.send_stall_alert("not-a-valid-url", "tok", "cell", 6, 1) is False


# ---- card #1135 / row #1813: route to the responsible peer at the FIRST alert ----

def _ask_body(board):
    import json
    asks = [c for c in board.calls if c[0] == "POST" and c[1].endswith("/ask")]
    assert len(asks) == 1, asks
    return json.loads(asks[0][2].decode())


def test_first_alert_names_the_stalled_cell_and_notifies_its_responsible_peer(monkeypatch):
    """The row exists at tick 60, names X, and is HELD by X's responsible peer, so
    someone who can act is told at once; the timeout is only a second escalation."""
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.is_alert_tick(60) is True          # the first alert tick
    assert st.send_stall_alert("http://gw", "tok", "cursor-win", 60, 3) is True
    body = _ask_body(board)
    assert body["holder"] == "lab-ovh"                       # notified now, as holder
    assert body["created_by"] == "cursor-win"                # X's own drain can close it
    assert "cell=cursor-win " in body["what"] and "cell=cursor-win " in body["accept"]
    assert body["holder"] != "cursor-win"                    # never held by the cell that cannot act
    assert body["fallback_target"] == "drop-on-meta-edge"    # the timeout is a SECOND escalation
    assert body["fallback_target"] != body["holder"]
    assert body["timeout_hours"] == 2


def test_responsible_peer_is_the_one_constant(monkeypatch):
    """Choice 1, ONE constant: flipping RESPONSIBLE_FOR_OWNERLESS changes the holder
    and nothing else has to change."""
    assert st.RESPONSIBLE_FOR_OWNERLESS == "lab-ovh"
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    assert _ask_body(board)["holder"] == "lab-ovh"

    board2 = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board2.handle)
    monkeypatch.setattr(st, "RESPONSIBLE_FOR_OWNERLESS", "science-claude")
    assert st.send_stall_alert("http://gw", "tok", "cell-y", 60, 1) is True
    body = _ask_body(board2)
    assert body["holder"] == "science-claude"
    assert body["fallback_target"] == "lab-ovh"          # a different peer than the holder


def test_commander_copy_is_off_by_default_and_one_constant(monkeypatch):
    """Choice 2, ONE constant: default no DM to the commander (card #989); flipping
    COMMANDER_COPY sends exactly one, still with one row."""
    assert st.COMMANDER_COPY is False
    off = _Board(allow_messages=True)
    monkeypatch.setattr(st.urllib.request, "urlopen", off.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 2) is True
    assert off.messages == []

    on = _Board(allow_messages=True)
    monkeypatch.setattr(st.urllib.request, "urlopen", on.handle)
    monkeypatch.setattr(st, "COMMANDER_COPY", True)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 2) is True
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 120, 2) is True   # row open: no second copy
    assert len(on.messages) == 1 and on.messages[0]["to_node"] == "commander"
    assert on.messages[0]["from_node"] == "cell-x"
    assert len(_asks(on)) == 1


def _asks(board):
    return [c for c in board.calls if c[0] == "POST" and c[1].endswith("/ask")]


def test_lab_ovh_own_stall_goes_to_drop_on_meta_edge(monkeypatch):
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "lab-ovh", 60, 1) is True
    body = _ask_body(board)
    assert body["holder"] == "drop-on-meta-edge"
    assert body["created_by"] == "lab-ovh"


def test_pending_n_does_not_count_the_rows_own_notice():
    """Minting the row DMs its holder (gateway /ask inserts one message per holder),
    so the notice can sit in a stuck queue. It must not inflate pending_n."""
    own = {"id": 91, "from_node": "cell-x", "to_node": "cell-x", "kind": "question",
           "content": "OFFERED - obligation on card 989, obligation #9\n\n"
                      "STALL card #989\ncell=cell-x ticks=60 pending_dms=3"}
    real = [{"id": i, "from_node": "lab-ovh", "to_node": "cell-x", "kind": "fyi",
             "content": f"hello {i}"} for i in (1, 2, 3)]
    other_cell = {"id": 92, "from_node": "cell-y", "to_node": "cell-y", "kind": "question",
                  "content": "STALL card #989\ncell=cell-y ticks=60 pending_dms=1"}
    assert st.pending_count(real, "cell-x") == 3
    assert st.pending_count(real + [own], "cell-x") == 3          # own notice not counted
    assert st.pending_count(real + [other_cell], "cell-x") == 4   # another cell's row is a real DM
    assert st.pending_count([], "cell-x") == 0
    assert st.pending_count(None, "cell-x") == 0


def test_owner_is_never_read_from_the_gateway(monkeypatch):
    """claude_peers.owner is a Meta-Edge USER id (a person). The alert must not
    look it up or route to it: no GET /peers at all."""
    board = _Board(owners={"cell-x": "some-human-sub"})
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    assert _ask_body(board)["holder"] == "lab-ovh"
    assert all("/peers" not in c[1] for c in board.calls)


def test_both_stall_call_sites_use_pending_count():
    """Guards the wiring: both loops pass the filtered count, not len(queue)."""
    import pathlib
    root = pathlib.Path(st.__file__).parent
    for rel in ("commands/daemon.py", "commands/mesh.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "stall_alert.pending_count(" in src, rel


# ---- card #1135 / row #1813, commander ruling 16:19Z: the cell's own `responsible:` key ----

def test_responsible_key_in_own_yaml_is_the_holder_at_the_first_alert(monkeypatch, _isolated_cells_dir):
    _cell_yaml(_isolated_cells_dir, "cursor-win", "name: cursor-win\nresponsible: workstation-lc\n")
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.is_alert_tick(60) is True
    assert st.send_stall_alert("http://gw", "tok", "cursor-win", 60, 2) is True
    body = _ask_body(board)
    assert body["holder"] == "workstation-lc"            # told at tick 60, not at a timeout
    assert body["created_by"] == "cursor-win"
    assert "cell=cursor-win " in body["what"]
    assert body["fallback_target"] == "lab-ovh"          # the later timeout is a second escalation
    assert body["timeout_hours"] == 2


def test_responsible_key_unset_falls_back_to_the_constant(monkeypatch, _isolated_cells_dir):
    _cell_yaml(_isolated_cells_dir, "cell-x", "name: cell-x\ngithub: someone\n")   # a yaml, no key
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    assert _ask_body(board)["holder"] == st.RESPONSIBLE_FOR_OWNERLESS == "lab-ovh"


def test_unreadable_yaml_falls_back_to_the_constant(monkeypatch, _isolated_cells_dir):
    (_isolated_cells_dir / "cell-x.yaml").write_bytes(b"name: [unterminated\n  responsible: {\n")
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    assert _ask_body(board)["holder"] == "lab-ovh"


def test_no_yaml_for_the_cell_falls_back_to_the_constant(monkeypatch, _isolated_cells_dir):
    _cell_yaml(_isolated_cells_dir, "someone-else", "name: someone-else\nresponsible: science-claude\n")
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    assert _ask_body(board)["holder"] == "lab-ovh"       # another cell's key is not this cell's


def test_responsible_naming_the_cell_itself_falls_back_to_the_constant(monkeypatch, _isolated_cells_dir):
    _cell_yaml(_isolated_cells_dir, "cell-x", "name: cell-x\nresponsible: cell-x\n")
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    body = _ask_body(board)
    assert body["holder"] == "lab-ovh"                   # a stalled cell cannot hold its own alert
    assert body["holder"] != body["created_by"]


def test_lab_ovh_own_stall_ignores_a_responsible_key(monkeypatch, _isolated_cells_dir):
    _cell_yaml(_isolated_cells_dir, "lab-ovh", "name: lab-ovh\nresponsible: science-claude\n")
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "lab-ovh", 60, 1) is True
    assert _ask_body(board)["holder"] == "drop-on-meta-edge"


@pytest.mark.parametrize("value", ["two words", "a/b", "", "x" * 80, "bad\nname"])
def test_a_value_that_is_not_a_peer_name_falls_back(monkeypatch, _isolated_cells_dir, value):
    import json as _json
    (_isolated_cells_dir / "cell-x.yaml").write_text(
        "name: cell-x\nresponsible: " + _json.dumps(value) + "\n", encoding="utf-8")
    board = _Board()
    monkeypatch.setattr(st.urllib.request, "urlopen", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    assert _ask_body(board)["holder"] == "lab-ovh"


def test_yaml_claims_takes_a_key_and_the_default_is_unchanged(_isolated_cells_dir):
    from swarph_cli import gh_identity
    _cell_yaml(_isolated_cells_dir, "cell-x", "name: cell-x\ngithub: gh-login\nresponsible: peer-y\n")
    d = _isolated_cells_dir
    assert [v for v, _ in gh_identity.yaml_claims("cell-x", d)[0]] == ["gh-login"]
    assert [v for v, _ in gh_identity.yaml_claims("cell-x", d, key="responsible")[0]] == ["peer-y"]


def test_a_cell_yaml_with_a_responsible_key_still_loads(_isolated_cells_dir):
    """`swarph spawn` reads the same file. An unknown top-level key must land in
    Cell.extra, not raise, or setting `responsible:` would break the cell."""
    from swarph_cli.cell import load_cell
    f = _isolated_cells_dir / "probe-cell.yaml"
    f.write_text("name: probe-cell\nrole: builder\ncwd: .\nresponsible: lab-ovh\n", encoding="utf-8")
    cell = load_cell(f)
    assert cell.name == "probe-cell"
    assert (cell.extra or {}).get("responsible") == "lab-ovh"
