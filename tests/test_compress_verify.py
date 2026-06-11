from pathlib import Path
from swarph_cli.compress import verify


def test_links_superset_pass():
    src = "- [a](a.md)\n- [b](b.md)\n"
    out = "- [a](a.md) hook\n- [b](b.md) hook\n"
    assert verify.links_preserved(src, out) is True


def test_links_superset_fail_on_dropped_link():
    src = "- [a](a.md)\n- [b](b.md)\n"
    out = "- [a](a.md)\n"           # b.md dropped
    assert verify.links_preserved(src, out) is False


def test_every_entry_has_resolvable_pointer(tmp_path):
    (tmp_path / "a.md").write_text("x")
    out = "- [a](a.md) hook\n"
    assert verify.entries_point_to_source(out, pointer="](", base=tmp_path) is True


def test_entry_without_pointer_fails(tmp_path):
    out = "- a hook with no link\n"
    assert verify.entries_point_to_source(out, pointer="](", base=tmp_path) is False


def test_redundancy_floor():
    verbose = ("The quick brown fox jumps over the lazy dog. " * 50)
    assert verify.redundancy_ratio(verbose) > 0.6   # very compressible
    dense = "phi+.226 eclipse-trap consensus-bull-loss diverged-win n88"
    assert verify.above_floor(dense, floor=0.45) is False  # already dense -> refuse


def test_idempotency_delta_noop():
    x = "- [a](a.md) terse hook\n- [b](b.md) terse hook\n"
    assert verify.idempotent(x, x) is True            # identical -> noop
    assert verify.idempotent(x, x[:len(x)//2]) is False  # big second-pass cut -> alarm
