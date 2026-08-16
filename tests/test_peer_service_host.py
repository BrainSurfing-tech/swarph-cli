import json

import pytest

from swarph_cli.peer_executor import (
    PeerExecutorError,
    PeerService,
    PeerSpool,
    envelope_digest,
    output_digest,
)
from swarph_cli.peer_service_host import (
    PeerPayload,
    PeerPayloadRequest,
    PeerServiceHost,
)


def _source_ref(fence=4):
    return {"queue_entry_id": 17, "source_dm_id": 17, "queue_claim_fence": fence}


def _job(source_ref=None):
    return {
        "schema_version": 1,
        "job_id": "dm-17",
        "source_dm_id": 17,
        "destination_peer": "gpt-lc",
        "delivery_ref": json.dumps(
            source_ref or _source_ref(), sort_keys=True, separators=(",", ":")
        ),
    }


class _Authorizer:
    def require_service(self, peer, spool_root):
        return None


class _Provider:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def get_payload(self, request):
        self.requests.append(request)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Handler:
    def __init__(self, result="handled"):
        self.result = result
        self.calls = []

    def handle(self, request, payload):
        self.calls.append((request, payload))
        return self.result


def _host(tmp_path, payload, *, job=None, handler=None):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(job or _job())
    service = PeerService(spool, "gpt-lc", _Authorizer())
    provider = _Provider(payload)
    handler = handler or _Handler()
    return PeerServiceHost(service, provider, handler), spool, provider, handler


def test_host_binds_payload_and_envelope_digests_into_receipt(tmp_path):
    payload = PeerPayload("gpt-lc", 17, 4, "bounded payload")
    host, spool, provider, handler = _host(tmp_path, payload)

    receipt = host.execute("dm-17")

    assert receipt["source_ref"] == _source_ref()
    assert receipt["payload_digest"] == output_digest(payload.text)
    assert receipt["envelope_digest"] == envelope_digest(_job())
    assert provider.requests == [PeerPayloadRequest("dm-17", "gpt-lc", 17, 4, 1)]
    assert handler.calls == [(provider.requests[0], payload.text)]
    persisted = "\n".join(
        path.read_text(encoding="utf-8") for path in spool.root.rglob("*.json")
    )
    assert payload.text not in persisted


@pytest.mark.parametrize(
    "payload",
    [
        PeerPayload("gpt-ops", 17, 4, "payload"),
        PeerPayload("gpt-lc", 18, 4, "payload"),
        PeerPayload("gpt-lc", 17, 5, "payload"),
    ],
)
def test_host_rejects_provider_binding_mismatch_before_handler(tmp_path, payload):
    host, spool, _, handler = _host(tmp_path, payload)

    with pytest.raises(PeerExecutorError, match="does not match"):
        host.execute("dm-17")

    assert handler.calls == []
    assert not list(spool.receipts.glob("*.json"))


def test_host_rejects_mismatched_envelope_provenance_before_provider(tmp_path):
    bad_ref = {"queue_entry_id": 18, "source_dm_id": 18, "queue_claim_fence": 4}
    payload = PeerPayload("gpt-lc", 17, 4, "payload")
    host, spool, provider, handler = _host(tmp_path, payload, job=_job(bad_ref))

    with pytest.raises(PeerExecutorError, match="source DM"):
        host.execute("dm-17")

    assert provider.requests == []
    assert handler.calls == []
    assert not list(spool.receipts.glob("*.json"))


def test_host_fails_closed_for_absent_or_failing_provider(tmp_path):
    spool = PeerSpool(tmp_path / "spool")
    spool.enqueue(_job())
    service = PeerService(spool, "gpt-lc", _Authorizer())
    with pytest.raises(PeerExecutorError, match="provider"):
        PeerServiceHost(service, None, _Handler())

    host, spool, _, handler = _host(
        tmp_path / "failing", RuntimeError("provider unavailable")
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        host.execute("dm-17")
    assert handler.calls == []
    assert not list(spool.receipts.glob("*.json"))
