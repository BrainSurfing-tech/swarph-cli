from swarph_cli import peer_service_reply as reply


def test_gateway_transport_posts_an_idempotent_answer(monkeypatch):
    captured = {}

    def post(url, body, token, *, timeout=10.0):
        captured.update(url=url, body=body, token=token)
        return 201, {"id": 17}

    monkeypatch.setattr(reply, "_post_json", post)
    reply.MeshGatewayReplyTransport("http://gw/", "secret", "service-peer").send(
        "source-peer", "reply", idempotency_key="a" * 64
    )

    assert captured["url"] == "http://gw/messages"
    assert captured["token"] == "secret"
    assert captured["body"] == {
        "from_node": "service-peer",
        "to_node": "source-peer",
        "kind": "answer",
        "content": "reply",
        "idempotency_key": "a" * 64,
    }


def test_gateway_transport_retains_error_detail(monkeypatch):
    monkeypatch.setattr(reply, "_post_json", lambda *args, **kwargs: (503, {"detail": "secret"}))

    try:
        reply.MeshGatewayReplyTransport("http://gw", "secret", "service-peer").send(
            "source-peer", "reply", idempotency_key="a" * 64
        )
    except reply.PeerReplyError as exc:
        assert str(exc) == "gateway reply delivery failed with status 503"
    else:
        raise AssertionError("expected a failed gateway delivery")
