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
        if url.endswith("/messages") or "/messages" in url:
            raise AssertionError(f"stall alert DMed instead of asking: {url}")

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
            row = {"id": self.next_id, "status": "open",
                   "holder": body["holder"], "what": body["what"],
                   "accept": body["accept"]}
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
    monkeypatch.setattr(st, "_open_url", board.handle)
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
    assert all("/messages" not in c[1] for c in board.calls)


def test_drain_closes_the_stall_row(monkeypatch):
    board = _Board()
    monkeypatch.setattr(st, "_open_url", board.handle)
    assert st.send_stall_alert("http://gw", "tok", "cell", 6, 2) is True
    assert st.clear_stall_alert("http://gw", "tok", "cell") is True
    assert all(r["status"] == "closed" for r in board.rows)
    assert st.clear_stall_alert("http://gw", "tok", "cell") is True
    closes = [c for c in board.calls if "/close" in c[1]]
    assert len(closes) == 1


def test_send_stall_alert_failsafe_on_error(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(st.urllib.request, "urlopen", boom)
    assert st.send_stall_alert("http://gw", "tok", "cell", 6, 1) is False


def test_send_stall_alert_failsafe_on_bad_gateway():
    # a schemeless / misconfigured gateway must be caught at Request-build time,
    # not raised (no monkeypatch — Request(...) itself raises ValueError).
    assert st.send_stall_alert("not-a-valid-url", "tok", "cell", 6, 1) is False
