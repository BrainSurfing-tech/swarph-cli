"""card #1073 — the organ triage router: typed-triage layer between sensors
and judges. Seven binding rulings (#1413); one test group per ruling.

Fail-first: written against the rulings before the router existed. The
fakes speak the real shapes: obligation rows as _row emits them, card
graphs as _graph emits them, the classifier seam as generate(prompt).
"""
from __future__ import annotations

import json
import os

import pytest

from swarph_cli.triage import backend as _backend
from swarph_cli.triage import job as _job
from swarph_cli.triage import router as _router
from swarph_cli.triage import sensor as _sensor
from swarph_cli.triage.rows import TriageStore

OWNERS = os.path.join(os.path.dirname(_router.__file__), "owners.json")
NOW = 1_790_000_000.0


def _iso(age_s):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - age_s, timezone.utc).isoformat()


def _now_iso():
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _owners():
    return _router.load_owners(OWNERS)


def _stuck_record(item_type="real-stall", sharp=True, holder="lab-ovh",
                  peer=True, item_id="stuck:1:2:validate", **extra):
    record = {
        "source_organ": "stuck-work", "type": item_type,
        "confidence": 1.0, "owner_hint": holder, "holder_is_peer": peer,
        "evidence": {"waiter_row": 1, "need_row": 2}, "item_id": item_id,
        "sharp": sharp, "security": False,
    }
    record.update(extra)
    return record


# ── ruling 1: identifiers-only items, owner map as data ──────────────────────


def test_evidence_carries_identifiers_only():
    ev = _sensor.stuck_evidence(4345, 4301)
    assert ev == {"waiter_row": 4345, "need_row": 4301}
    assert "payload" not in ev
    blob = json.dumps(ev)
    assert "sk-ant" not in blob and "巡" not in blob


def test_owner_map_is_data_not_code():
    owners = _owners()
    assert isinstance(owners["owners"], dict)
    # no owner literal lives in the router module itself
    source = open(_router.__file__, encoding="utf-8").read()
    for name in ("drop-on-meta-edge", "science-claude"):
        assert name not in source


def test_unmapped_organ_routes_to_lab_and_counts():
    store = TriageStore(_backend.MemoryBackend())
    summary = _job.route_and_store(
        store, _owners(),
        [{"source_organ": "brand-new-organ", "type": "real-stall",
          "confidence": 1.0, "owner_hint": None, "holder_is_peer": False,
          "evidence": {"waiter_row": 9, "need_row": 3},
          "item_id": "x:9:3", "sharp": True, "security": False,
          "unmapped": True}])
    assert summary["appended"] == 1
    row = store.owner_row("lab-ovh")
    assert len(store.backend.list_posts(row)) == 1
    assert store.footer()["unmapped"] == {"brand-new-organ": 1}
    assert store.footer()["split"] == {}


def test_stuck_unresolvable_holder_routes_to_lab_and_counts():
    store = TriageStore(_backend.MemoryBackend())
    summary = _job.route_and_store(
        store, _owners(), [_stuck_record(holder=None, peer=False)])
    assert summary["appended"] == 1
    assert len(store.backend.list_posts(store.owner_row("lab-ovh"))) == 1
    assert store.footer()["unmapped"] == {"stuck-work": 1}


# ── ruling 2: thresholds in data, no literals, rules sharp ──────────────────


def test_no_confidence_literal_in_router_or_store():
    import re
    for path in (_router.__file__, __import__(
            "swarph_cli.triage.rows", fromlist=["x"]).__file__):
        source = open(path, encoding="utf-8").read()
        for match in re.finditer(r"(?<!\w)0\.\d+", source):
            # version comments and the 1.0 certainty marker are the only
            # allowed decimals; every other float literal is a cutoff.
            assert match.group(0) == "1.0", (path, match.group(0))


def test_model_result_splits_until_proven_calibrates():
    class Fake:
        def generate(self, prompt):
            return '{"label": "threat", "confidence": 0.99}'

    label, conf, sharp, called = _router.classify_model(
        "security", "some payload text", ["threat", "confident-benign"],
        _owners()["thresholds"], Fake())
    assert called is True
    assert label == "threat" and conf == 0.99
    assert sharp is False  # no calibrated threshold yet


def test_model_result_sharp_with_calibrated_threshold(tmp_path):
    local = tmp_path / "owners.local.json"
    local.write_text(json.dumps({"thresholds": {"security": 0.5}}),
                     encoding="utf-8")

    class Fake:
        def generate(self, prompt):
            return '{"label": "threat", "confidence": 0.9}'

    owners = _router.load_owners(OWNERS, str(local))
    label, _conf, sharp, _called = _router.classify_model(
        "security", "payload", ["threat", "confident-benign"],
        owners["thresholds"], Fake())
    assert (label, sharp) == ("threat", True)


def test_model_below_threshold_splits():
    class Fake:
        def generate(self, prompt):
            return '{"label": "threat", "confidence": 0.2}'

    label, _conf, sharp, called = _router.classify_model(
        "security", "payload", ["threat", "confident-benign"],
        {"security": 0.5}, Fake())
    assert called is True and sharp is False


def test_model_garbage_splits_without_calling_further():
    class Exploding:
        calls = 0

        def generate(self, prompt):
            type(self).calls += 1
            raise RuntimeError("wedged queue")

    for payload, client in (("payload", Exploding()),
                            (b"\x00\x01 not text", Exploding()),
                            (None, Exploding())):
        label, conf, sharp, called = _router.classify_model(
            "security", payload, ["threat", "confident-benign"], {}, client)
        assert (label, conf, sharp) == ("split", None, False)
    assert Exploding.calls == 1  # bytes/None never reach the model


def test_stuck_rules_sharp_without_threshold():
    assert _router.classify_stuck_row("open", None, _now_iso()) == (
        "real-stall", 1.0, True)
    assert _router.classify_stuck_row(
        "open", "2026-11-27T00:00:00Z", _now_iso())[0] == "deliberate-wait"
    assert _router.classify_stuck_row("closed", None, _now_iso()) == (
        "needed-row-closed-race", 1.0, True)


# ── ruling 3: deliberate-wait via the NEED card's due_at ─────────────────────


def test_deliberate_wait_reads_need_card_due_at_not_links():
    classified = _sensor.map_stuck_finding(
        {"blocker": "lab-ovh", "id": 1, "need": "validate"},
        "open", "2026-11-27T00:00:00Z", _now_iso())
    assert classified["type"] == "deliberate-wait"
    assert classified["sharp"] is True


def test_no_due_at_stays_real_stall():
    classified = _sensor.map_stuck_finding(
        {"blocker": "lab-ovh", "id": 1, "need": "validate"},
        "open", None, _now_iso())
    assert classified["type"] == "real-stall"


def test_closed_need_is_race_no_dm_no_append():
    store = TriageStore(_backend.MemoryBackend())
    sent = []

    def dm_sender(record, owner):
        sent.append(record)
        return True

    summary = _job.route_and_store(
        store, _owners(), [_stuck_record("needed-row-closed-race")],
        dm_sender=dm_sender)
    assert summary["appended"] == 0 and sent == []
    assert store.footer()["closed_race"] == 1


# ── ruling 4: one open row per owner, cap 10, carried, security first ────────


def test_one_open_row_carried_never_duplicated():
    store = TriageStore(_backend.MemoryBackend())
    owners = _owners()
    first = _job.route_and_store(store, owners, [_stuck_record()])
    second = _job.route_and_store(store, owners, [_stuck_record()])
    assert (first["appended"], second["appended"]) == (1, 0)
    assert second["duplicate"] == 1
    assert len(store.backend.list_posts(store.owner_row("lab-ovh"))) == 1


def test_row_caps_at_ten_with_overflow_named():
    store = TriageStore(_backend.MemoryBackend())
    records = [_stuck_record(item_id=f"stuck:1:{i}:s") for i in range(12)]
    summary = _job.route_and_store(store, _owners(), records)
    assert summary["appended"] == 10
    assert summary["overflow"] == 2
    assert store.footer()["overflow"] == 2
    assert len(store.backend.list_posts(store.owner_row("lab-ovh"))) == 10


def test_accept_lines_capped_and_added_only():
    from swarph_cli.triage.router import ACCEPT_MAX
    from swarph_cli.triage import rows as _rows
    assert len(_rows.take_accept("id", "x" * 5000)) <= ACCEPT_MAX
    assert _rows.take_accept("stuck:1:2", "real-stall").startswith("ACCEPT ")


# ── ruling 5: confident-benign in the footer ─────────────────────────────────


def test_confident_benign_never_takes_a_slot():
    store = TriageStore(_backend.MemoryBackend())
    records = [{"source_organ": "security", "type": "confident-benign",
                "confidence": 0.99, "owner_hint": None,
                "holder_is_peer": False,
                "evidence": {"path": "src/x.py", "line": 3},
                "item_id": "benign:src/x.py:3", "sharp": True,
                "security": False}
               for _ in range(3)]
    summary = _job.route_and_store(store, _owners(), records)
    assert summary["appended"] == 0
    assert store.footer()["confident_benign"] == 3
    assert store.footer()["benign_sample"] == ["benign:src/x.py:3"]


# ── ruling 6: never close/edit/delete; DM counting; race counters ───────────


def test_router_has_no_close_edit_delete_path():
    import re
    for mod in (_router, _job):
        source = open(mod.__file__, encoding="utf-8").read()
        assert not re.search(r"def (close|delete|edit|remove)_", source)


def test_only_sharp_real_stall_may_dm_and_dm_is_counted():
    store = TriageStore(_backend.MemoryBackend())
    sent = []

    def dm_sender(record, owner):
        sent.append((record["type"], owner))
        return True

    records = [_stuck_record(),
               _stuck_record("deliberate-wait", item_id="stuck:1:3:d"),
               _stuck_record("real-stall", sharp=False,
                             item_id="stuck:1:4:s")]
    summary = _job.route_and_store(store, _owners(), records,
                                   dm_sender=dm_sender)
    assert sent == [("real-stall", "lab-ovh")]
    assert summary["dm_sent"] == 1
    assert store.footer()["dm_sent"] == 1
    assert store.footer()["split"] == {"stuck-work": 1}


def test_security_item_never_on_uncounted_drop_path():
    store = TriageStore(_backend.MemoryBackend())
    record = _stuck_record(item_id="sec:1:2")
    record.update({"source_organ": "security", "security": True,
                   "race": "missing-need"})
    summary = _job.route_and_store(store, _owners(), [record])
    assert summary["appended"] == 1
    assert store.footer()["race"] == {}


def test_non_security_race_counted_per_organ():
    store = TriageStore(_backend.MemoryBackend())
    record = _stuck_record(item_id="stuck:1:9:m")
    record.update({"type": "race", "race": "missing-need", "sharp": False})
    _job.route_and_store(store, _owners(), [record])
    assert store.footer()["race"] == {"stuck-work": 1}


# ── ruling 7: scrub before model; rules never see payload ────────────────────


def test_scrub_redacts_secrets_before_model():
    seen = {}

    class Fake:
        def generate(self, prompt):
            seen["prompt"] = prompt
            return '{"label": "confident-benign", "confidence": 0.9}'

    _router.classify_model(
        "security", "token sk-ant-aaaa-bbbb-cccc-dddd here",
        ["threat", "confident-benign"], {}, Fake())
    assert "sk-ant-aaaa" not in seen["prompt"]
    assert "REDACTED" in seen["prompt"]


def test_unscrubbable_payload_never_reaches_model():
    class Fake:
        def generate(self, prompt):
            raise AssertionError("model must not be called")

    _label, _conf, sharp, called = _router.classify_model(
        "security", {"nested": ["not", "text"]},
        ["threat", "confident-benign"], {}, Fake())
    assert (sharp, called) == (False, False)
