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
    assert captured.get("chain_token") in (None, "")
