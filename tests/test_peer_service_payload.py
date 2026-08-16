import json

import pytest

from swarph_cli.peer_executor import PeerExecutorError
from swarph_cli.peer_service_host import PeerPayloadRequest
from swarph_cli.peer_service_payload import InboxLogPeerPayloadProvider


def _request(**changes):
    values = {
        "job_id": "dm-17",
        "peer": "gpt-lc",
        "source_dm_id": 17,
        "queue_claim_fence": 4,
        "service_fencing_token": 1,
    }
    values.update(changes)
    return PeerPayloadRequest(**values)


def _write_log(path, *records):
    path.write_text(
        "\n".join(record if isinstance(record, str) else json.dumps(record) for record in records)
        + "\n",
        encoding="utf-8",
    )


def test_provider_returns_only_the_exact_recipient_record(tmp_path):
    log = tmp_path / "inbox.log"
    _write_log(
        log,
        {"id": 16, "to_node": "gpt-lc", "from_node": "gpt-ops", "content": "older"},
        {"id": 18, "to_node": "gpt-ops", "from_node": "gpt-lc", "content": "other recipient"},
        {"id": 17, "to_node": "gpt-lc", "from_node": "gpt-ops", "content": "exact body"},
    )

    payload = InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(_request())

    assert payload.peer == "gpt-lc"
    assert payload.source_dm_id == 17
    assert payload.queue_claim_fence == 4
    assert payload.text == "exact body"


def test_provider_tolerates_identical_monitor_replay_but_rejects_conflict(tmp_path):
    record = {"id": 17, "to_node": "gpt-lc", "from_node": "gpt-ops", "content": "same"}
    log = tmp_path / "inbox.log"
    _write_log(log, record, record)
    assert InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(_request()).text == "same"

    _write_log(log, record, {**record, "content": "changed"})
    with pytest.raises(PeerExecutorError, match="conflicting"):
        InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(_request())


@pytest.mark.parametrize(
    "records,error",
    [
        (["not json", {"id": 17, "to_node": "gpt-lc", "from_node": "gpt-ops"}], "invalid"),
        ([{"id": 17, "to_node": "gpt-ops", "from_node": "gpt-lc", "content": "wrong"}], "wrong recipient"),
        ([{"id": 18, "to_node": "gpt-lc", "from_node": "gpt-ops", "content": "other"}], "absent"),
    ],
)
def test_provider_fails_closed_for_missing_or_mismatched_source_records(tmp_path, records, error):
    log = tmp_path / "inbox.log"
    _write_log(log, *records)

    with pytest.raises(PeerExecutorError, match=error):
        InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(_request())


def test_provider_rejects_wrong_peer_and_oversized_archive(tmp_path):
    log = tmp_path / "inbox.log"
    _write_log(log, {"id": 17, "to_node": "gpt-lc", "from_node": "gpt-ops", "content": "body"})

    with pytest.raises(PeerExecutorError, match="identity"):
        InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(_request(peer="gpt-ops"))
    with pytest.raises(PeerExecutorError, match="read bound"):
        InboxLogPeerPayloadProvider(log, "gpt-lc", max_log_bytes=1).get_payload(_request())


def test_provider_binds_new_routed_jobs_to_the_original_sender(tmp_path):
    log = tmp_path / "inbox.log"
    _write_log(log, {"id": 17, "to_node": "gpt-lc", "from_node": "other-peer", "content": "body"})

    with pytest.raises(PeerExecutorError, match="wrong sender"):
        InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(
            _request(source_peer="gpt-ops")
        )

    _write_log(log, {"id": 17, "to_node": "gpt-lc", "from_node": "gpt-ops", "content": "body"})
    payload = InboxLogPeerPayloadProvider(log, "gpt-lc").get_payload(
        _request(source_peer="gpt-ops")
    )
    assert payload.source_peer == "gpt-ops"
