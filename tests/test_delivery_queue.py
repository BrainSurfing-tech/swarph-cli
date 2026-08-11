import pytest

from swarph_cli.delivery_queue import DeliveryQueue, DeliveryQueueError, wake_for


def _dm(i, kind="fyi", thread_id=None):
    return {"id": i, "from_node": "peer", "kind": kind,
            "thread_id": thread_id, "content": f"m{i}"}


def test_wake_for_rules():
    assert wake_for("question", None) is True
    assert wake_for("unblock", None) is True
    assert wake_for("answer", "t1") is True     # threaded answer = targeted
    assert wake_for("answer", None) is False    # broadcast answer = ride-along
    assert wake_for("fyi", None) is False
    assert wake_for("status", "t9") is False    # status never wakes


def test_enqueue_and_pending(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1, "question"))
    q.enqueue(_dm(2, "fyi"))
    p = q.pending()
    assert [e["id"] for e in p] == [1, 2]
    assert p[0]["wake"] is True and p[1]["wake"] is False
    assert q.any_wake() is True


def test_enqueue_dedups_by_id(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1))
    q.enqueue(_dm(1))
    assert len(q.pending()) == 1


def test_persist_across_reload(tmp_path):
    p = tmp_path / "q.json"
    q = DeliveryQueue(p)
    q.enqueue(_dm(1, "unblock"))
    q.bump_deferred()
    q2 = DeliveryQueue(p)                 # fresh instance reads the file
    assert [e["id"] for e in q2.pending()] == [1]
    assert q2.deferred_ticks == 1


def test_id_only_remove_is_forbidden_and_reset_is_available(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1)); q.enqueue(_dm(2))
    q.bump_deferred(); q.bump_deferred()
    with pytest.raises(DeliveryQueueError, match="ID-only"):
        q.remove({1})
    q.reset_deferred()
    assert [e["id"] for e in q.pending()] == [1, 2]
    assert q.deferred_ticks == 0


def test_unreceipted_job_remains_owed_and_unread(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(17, "question"))
    q.record_eligibility(17, "eligible", "question is assigned to the service")
    job = q.claim_for_service(17, "gpt-lc", max_active=1)

    assert job["source_dm_id"] == 17
    assert q.pending()[0]["source_read_state"] == "unread"
    assert q.status(now=q.pending()[0]["queued_at"] + 5)["owed"] == 1
    assert q.accepted_receipts() == []


def test_receipt_must_bind_job_dm_peer_token_digest_and_provenance(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(17, "question"))
    q.record_eligibility(17, "eligible", "question is assigned to the service")
    job = q.claim_for_service(17, "gpt-lc", max_active=1)
    receipt = job | {"output_digest": "a" * 64}

    with pytest.raises(DeliveryQueueError, match="does not match"):
        q.remove_on_receipt(receipt | {"fencing_token": 2})
    assert q.status()["owed"] == 1

    q.remove_on_receipt(receipt)
    assert q.status()["owed"] == 0
    assert q.accepted_receipts() == [receipt]


def test_cannot_evaluate_and_capacity_refusal_are_loud_and_persisted(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1, "question"))
    q.enqueue(_dm(2, "question"))
    q.record_eligibility(1, "eligible", "service policy permits it")
    q.record_eligibility(2, "eligible", "service policy permits it")
    q.claim_for_service(1, "gpt-lc", max_active=1)
    assert q.claim_for_service(2, "gpt-lc", max_active=1) is None

    status = q.status()
    assert status["eligibility"]["cannot_evaluate"] == 0
    assert status["service_state"]["claimed"] == 1
    assert status["service_state"]["capacity_refused"] == 1
    assert "capacity exhausted" in q.pending()[1]["capacity_refusal"]


def test_cannot_evaluate_is_surfaceable_with_oldest_age(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1, "question"))
    queued_at = q.pending()[0]["queued_at"]
    status = q.status(now=queued_at + 12.5)
    assert status["eligibility"]["cannot_evaluate"] == 1
    assert status["oldest_age_seconds"] == 12.5


def test_legacy_queue_entries_migrate_to_unread_cannot_evaluate(tmp_path):
    path = tmp_path / "q.json"
    path.write_text('{"pending": [{"id": 1}], "deferred_ticks": 0}')
    q = DeliveryQueue(path)
    entry = q.pending()[0]
    assert entry["eligibility"] == "cannot_evaluate"
    assert entry["service_state"] == "unassigned"
    assert entry["source_read_state"] == "unread"


def test_returned_job_cannot_mutate_the_authoritative_provenance(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1, "question"))
    q.record_eligibility(1, "eligible", "service policy permits it")
    job = q.claim_for_service(1, "gpt-lc", max_active=1)
    job["reply_provenance"]["actor_id"] = "forged"
    assert q.pending()[0]["job"]["reply_provenance"]["actor_id"] == "gpt-lc/service"


def test_corrupt_file_is_empty_failsafe(tmp_path):
    p = tmp_path / "q.json"
    p.write_text("{not json")
    q = DeliveryQueue(p)                  # must not raise
    assert q.pending() == []
    assert q.deferred_ticks == 0


def test_valid_json_wrong_shape_is_empty_failsafe(tmp_path):
    # a torn write can leave syntactically valid JSON of the wrong shape;
    # must be treated as empty, never raise (never lose the daemon at startup).
    for bad in ("null", "[1,2,3]", '"a string"', "42"):
        p = tmp_path / "q.json"
        p.write_text(bad)
        q = DeliveryQueue(p)
        assert q.pending() == []
        assert q.deferred_ticks == 0


def test_pending_is_defensive_copy(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1))
    q.pending()[0]["content"] = "MUTATED"     # caller mutation must not leak
    assert q.pending()[0]["content"] == "m1"


def test_load_logs_on_corrupt_file(tmp_path, capsys):
    p = tmp_path / "q.json"
    p.write_text("{not json")
    DeliveryQueue(p)                     # corruption → reset + LOG
    assert "delivery queue unreadable" in capsys.readouterr().err


def test_load_silent_on_first_run(tmp_path, capsys):
    DeliveryQueue(tmp_path / "nope.json")   # FileNotFoundError → normal first run
    assert capsys.readouterr().err == ""    # must NOT log
