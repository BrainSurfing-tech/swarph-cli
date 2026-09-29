import swarph_cli.stall_alert as st


def test_is_alert_tick_backoff_sequence():
    fire = [n for n in range(1, 100) if st.is_alert_tick(n)]
    assert fire == [6, 12, 24, 48, 96]


def test_is_alert_tick_below_threshold():
    assert not any(st.is_alert_tick(n) for n in range(0, 6))


class _Board:
    """One card's obligations. A second ask while one is open is the defect."""

    def __init__(self):
        self.rows = []
        self.calls = []
        self.next_id = 1

    def handle(self, req, timeout=None):
        import json
        url = req.full_url
        self.calls.append((req.get_method(), url, req.data))
        if "/messages" in url:
            raise AssertionError(f"main DMs commander: {url}")

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
    assert body["timeout_hours"] == 24
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
