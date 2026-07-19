import swarph_cli.stall_alert as st


def test_is_alert_tick_backoff_sequence():
    fire = [n for n in range(1, 100) if st.is_alert_tick(n)]
    assert fire == [6, 12, 24, 48, 96]


def test_is_alert_tick_below_threshold():
    assert not any(st.is_alert_tick(n) for n in range(0, 6))


def test_send_stall_alert_posts_unblock(monkeypatch):
    import json
    captured = {}

    class _Resp:
        status = 200
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(st.urllib.request, "urlopen", fake_urlopen)
    ok = st.send_stall_alert("http://gw", "tok", "workstation-lc", 12, 3)
    assert ok is True
    assert captured["url"] == "http://gw/messages"
    body = json.loads(captured["body"].decode())
    assert body["to_node"] == "commander"
    assert body["kind"] == "unblock"
    assert body["from_node"] == "workstation-lc"
    assert "workstation-lc" in body["content"]


def test_send_stall_alert_failsafe_on_error(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(st.urllib.request, "urlopen", boom)
    assert st.send_stall_alert("http://gw", "tok", "cell", 6, 1) is False
