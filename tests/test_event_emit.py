def test_event_emit_posts_with_tag_and_token(monkeypatch):
    from swarph_cli.commands import event as evmod
    captured = {}
    monkeypatch.setattr(evmod, "_post_channel", lambda **kw: captured.update(kw) or 0)
    rc = evmod.run_event(["emit", "stage-done", "the output", "--chain-token", "TOK", "--channel", "events"])
    assert rc == 0
    assert captured["channel"] == "events"
    assert captured["event"] == "stage-done"
    assert captured["payload"] == "the output"
    assert captured["chain_token"] == "TOK"

def test_event_emit_no_token(monkeypatch):
    from swarph_cli.commands import event as evmod
    captured = {}
    monkeypatch.setattr(evmod, "_post_channel", lambda **kw: captured.update(kw) or 0)
    evmod.run_event(["emit", "e", "p"])
    assert captured.get("chain_token") is None  # argparse default=None, not ""


def test_event_emit_real_envelope_is_sentinel_wrapped(monkeypatch):
    # exercise the REAL _post_channel (not the seam) to prove the content envelope is
    # sentinel-wrapped so P1's guard identifies events unambiguously (a plain channel
    # message won't carry the top-level `swarph_event` key). The CLI chain token is
    # attached on the bytes after redaction, not inside the pre-redact body.
    import json
    from swarph_cli.commands import event as evmod
    monkeypatch.setattr(evmod, "resolve_self_name", lambda *a, **k: "lab-ovh")
    monkeypatch.setattr(evmod, "resolve_token", lambda *a, **k: "tok")
    captured = {}

    def _capture(url, body, token, encoded=None, timeout=10.0):
        captured.update(body=body, encoded=encoded)
        return 200, {"id": 1}

    monkeypatch.setattr(evmod, "post_json", _capture)
    rc = evmod.run_event(["emit", "stage-done", "out", "--chain-token", "TOK"])
    assert rc == 0
    content = json.loads(captured["body"]["content"])
    assert "swarph_event" in content
    assert content["swarph_event"] == {"event": "stage-done", "payload": "out"}
    assert captured["body"]["kind"] == "fyi"  # VALID_KINDS-safe; P1 promotes to first-class
    wire = json.loads(json.loads(captured["encoded"])["content"])
    assert wire["swarph_event"] == {"event": "stage-done", "payload": "out", "chain_token": "TOK"}


def test_chain_token_reaches_the_wire_and_the_payload_is_still_scrubbed(monkeypatch):
    """The CLI --chain-token is attached after redaction, like _search.

    A producer-signed token and a dotless credential-shaped token both reach
    the wire. A classic key in the payload does not. A pasted chain_token
    field is not this bypass.
    """
    import json
    import secrets

    from swarph_cli.chain_token import sign_chain_token
    from swarph_cli.commands import event as evmod
    from swarph_cli.redact import redact_text

    signed = sign_chain_token("producer-secret-xyz", "chain-1", 1, "jti-abc")
    opaque = None
    for _ in range(40):
        candidate = secrets.token_urlsafe(32)
        if redact_text(candidate, caller="t") != candidate:
            opaque = candidate
            break
    assert opaque is not None
    classic = "sk-" + ("cd" * 12)

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id": 7}'

    def emit(chain):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["raw"] = req.data
            return _Resp()

        monkeypatch.setattr(
            "swarph_cli.commands.mesh.urllib.request.urlopen", fake_urlopen
        )
        monkeypatch.setattr(evmod, "resolve_self_name", lambda *a, **k: "friendly-coder")
        monkeypatch.setattr(evmod, "resolve_token", lambda *a, **k: "tok")
        # chain can start with '-' (signed JWT or urlsafe); two-arg form breaks
        # argparse on Windows (looks like another flag).
        rc = evmod.run_event([
            "emit", "stage-done", "note " + classic,
            f"--chain-token={chain}",
            "--gateway", "http://127.0.0.1:9",
        ])
        assert rc == 0, rc
        body = json.loads(captured["raw"].decode("utf-8"))
        return json.loads(body["content"])["swarph_event"]

    for chain in (signed, opaque):
        ev = emit(chain)
        assert ev["chain_token"] == chain
        assert classic not in ev["payload"]
        assert "REDACTED" in ev["payload"]


def test_chain_token_on_unparseable_body_refuses_and_sends_nothing(monkeypatch, capsys):
    """Redaction can break the content JSON (password="}, PASSWORD=", api_key=").

    --chain-token must refuse by name, send nothing, and exit nonzero.
    No traceback, and neither the payload nor the token reaches the stubbed wire.
    """
    from swarph_cli.commands import event as evmod

    token = "chain-token-must-not-leak"
    payloads = (
        'password="}',
        'export PASSWORD=" x',
        'api_key="',
    )
    calls = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id": 1}'

    def fake_urlopen(req, timeout=None):
        calls.append(req.data or b"")
        return _Resp()

    monkeypatch.setattr(
        "swarph_cli.commands.mesh.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setattr(evmod, "resolve_self_name", lambda *a, **k: "friendly-coder")
    monkeypatch.setattr(evmod, "resolve_token", lambda *a, **k: "tok")

    for payload in payloads:
        before = len(calls)
        rc = evmod.run_event([
            "emit", "stage-done", payload,
            "--chain-token", token,
            "--gateway", "http://127.0.0.1:9",
        ])
        captured = capsys.readouterr()
        assert rc != 0
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out
        assert "_attach_chain_token refused: JSONDecodeError" in captured.err
        assert token not in captured.err
        assert token not in captured.out
        assert payload not in captured.err
        assert payload not in captured.out
        assert calls[before:] == []

    assert calls == []
