"""#84 CLI half — `--label` filter, `--label` on add, `cards label add/rm`.

The gateway takes a FULL REPLACEMENT for labels (they are a set — a merge-only
field could never REMOVE one), so `label rm` is unavoidably read-modify-write.
The risk that creates is the one these tests pin: a replacement built on a FAILED
read would silently drop every other label on the card.
"""
import pytest

from swarph_cli.commands import board as b


# ── the pure transform, so the RMW is testable without a gateway ────────────

@pytest.mark.parametrize("cur,action,lab,want", [
    ([], "add", "obligation-ledger", ["obligation-ledger"]),
    (["a"], "add", "b", ["a", "b"]),
    (["a", "b"], "rm", "a", ["b"]),
    (["a"], "rm", "nope", ["a"]),                      # removing an absent label is a no-op
    (["a"], "add", "a", ["a"]),                        # idempotent add
    (["a"], "add", "A", ["a"]),                        # case is a WRITING variation, not a new label
    (["A", "B"], "rm", "a", ["b"]),                    # ...on both sides
    (None, "add", "x", ["x"]),                         # a card with no labels column yet
])
def test_apply_label(cur, action, lab, want):
    assert b._apply_label(cur, action, lab) == want


def test_add_is_idempotent_and_order_preserving():
    """Labels are a SET but the ORDER is what a human reads. Re-adding must not
    reorder — a card whose labels shuffle on every touch is noise in the diff."""
    out = ["one", "two", "three"]
    for lab in ("one", "two", "three"):
        out = b._apply_label(out, "add", lab)
    assert out == ["one", "two", "three"]


# ── the URL + payload builders ─────────────────────────────────────────────

def test_list_url_carries_the_label_filter():
    url = b._cards_list_url("http://gw", project=8, label="obligation-ledger")
    assert "label=obligation-ledger" in url
    assert "project=8" in url


def test_list_url_omits_the_filter_when_absent():
    """NON-VACUITY: an empty filter must not become `?label=`, which the gateway
    would read as a request for cards labelled with the empty string."""
    assert "label" not in b._cards_list_url("http://gw", project=8)
    assert "label" not in b._cards_list_url("http://gw", project=8, label=None)
    assert "label" not in b._cards_list_url("http://gw", project=8, label="")


def test_add_payload_carries_labels_only_when_given():
    assert "labels" not in b._card_add_payload("me", 8, "t")
    p = b._card_add_payload("me", 8, "t", labels=["a", "b"])
    assert p["labels"] == ["a", "b"]


# ── >>> THE CLOBBER GUARD — the reason the RMW is safe <<< ──────────────────

def test_label_REFUSES_to_write_when_the_read_FAILS(monkeypatch, capsys):
    """>>> A REPLACEMENT BUILT ON A FAILED READ WOULD DROP EVERY OTHER LABEL. <<<

    `label add` must GET the card, compute the new set, then PATCH the whole
    array. If the GET fails and the code proceeds with `[]` as "current", the
    PATCH silently erases the card's other labels — a data-loss bug that looks
    like a successful write.

    Pinned by making the GET fail and asserting NOTHING is patched.
    """
    patched = []
    monkeypatch.setattr(b, "_http_get_json", lambda *a, **k: (503, {"detail": "down"}))
    monkeypatch.setattr(b, "_patch_json", lambda *a, **k: patched.append(a) or (200, {}))
    monkeypatch.setattr(b, "_resolve_self_name", lambda *_: "lab-ovh")
    monkeypatch.setattr(b, "_resolve_token", lambda *_: "tok")

    rc = b.run_board(["cards", "label", "add", "1", "x", "--gateway", "http://gw"])
    assert rc == 1, "a failed read must refuse, not fall through to a write"
    assert patched == [], "PATCHED after a failed GET — the other labels were clobbered"
    assert "refusing to write" in capsys.readouterr().err
