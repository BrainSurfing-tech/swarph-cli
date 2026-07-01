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
    # message won't carry the top-level `swarph_event` key).
    import json
    from swarph_cli.commands import event as evmod
    monkeypatch.setattr(evmod, "resolve_self_name", lambda *a, **k: "lab-ovh")
    monkeypatch.setattr(evmod, "resolve_token", lambda *a, **k: "tok")
    captured = {}
    monkeypatch.setattr(evmod, "post_json",
                        lambda url, body, token: (captured.update(body=body) or (200, {"id": 1})))
    rc = evmod.run_event(["emit", "stage-done", "out", "--chain-token", "TOK"])
    assert rc == 0
    content = json.loads(captured["body"]["content"])
    assert "swarph_event" in content
    assert content["swarph_event"] == {"event": "stage-done", "payload": "out", "chain_token": "TOK"}
    assert captured["body"]["kind"] == "fyi"  # VALID_KINDS-safe; P1 promotes to first-class
