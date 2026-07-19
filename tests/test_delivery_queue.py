from swarph_cli.delivery_queue import DeliveryQueue, wake_for


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


def test_remove_and_reset(tmp_path):
    q = DeliveryQueue(tmp_path / "q.json")
    q.enqueue(_dm(1)); q.enqueue(_dm(2))
    q.bump_deferred(); q.bump_deferred()
    q.remove({1})
    q.reset_deferred()
    assert [e["id"] for e in q.pending()] == [2]
    assert q.deferred_ticks == 0


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
