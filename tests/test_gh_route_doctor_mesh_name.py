"""gh-route doctor must enumerate cells by their MESH NAME, not their filename.

>>> THE ROUTER RESOLVES $SWARPH_SELF. A PREFLIGHT KEYED ON THE FILENAME IS ASKING A
DIFFERENT QUESTION THAN THE THING IT CERTIFIES. <<<

Found on lab-ovh 2026-08-18. `lab.yaml` carries `name: lab-ovh` with `role: lab` — a
deliberate split (commander, 2026-05-28: "I want lab to be lab"). Doctor reported
`lab -> (unmapped)` while SWARPH_SELF=lab-ovh was mapped and resolved fine.

Both directions are wrong, and the second is why this is a test and not a tidy-up:

    FALSE POSITIVE   doctor cries refusal for a cell that would resolve
    FALSE NEGATIVE   "fix" it by adding the FILENAME as a key -> doctor goes GREEN
                     while the router still looks up the mesh name. A preflight that
                     certifies an install which then refuses every gh call.

A green that means nothing is worse than a red that means nothing, which is why the
false-negative direction gets its own test below.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from swarph_cli.commands import gh_route


def _box(tmp_path, monkeypatch, files: dict):
    d = tmp_path / "cells"
    d.mkdir()
    for fname, body in files.items():
        (d / fname).write_text(body, encoding="utf-8")
    monkeypatch.setattr(gh_route, "cells_dir", lambda: d)
    return d


def test_a_cell_whose_FILENAME_differs_from_its_mesh_name_reports_the_MESH_NAME(
        tmp_path, monkeypatch):
    """The real lab-ovh shape: lab.yaml declaring name: lab-ovh, with an inline
    comment after the value — these configs carry them, and a naive split(':')[1]
    would return the comment too."""
    _box(tmp_path, monkeypatch, {
        "lab.yaml": "name: lab-ovh   # mesh peer name\nrole: lab   # display\n",
    })

    assert gh_route._cells_on_this_box() == ["lab-ovh"], \
        "doctor must report the key the ROUTER resolves, not the filename"


def test_the_filename_is_used_only_when_the_config_declares_no_name(tmp_path, monkeypatch):
    """NON-VACUITY. If the fix returned the stem unconditionally the test above fails;
    if it returned None-or-name unconditionally, a nameless config vanishes from the
    preflight — and a cell missing from the census is exactly the population error the
    box-wide check exists to prevent."""
    _box(tmp_path, monkeypatch, {"nameless.yaml": "role: whatever\n"})

    assert gh_route._cells_on_this_box() == ["nameless"]


def test_both_shapes_together_are_each_reported_correctly(tmp_path, monkeypatch):
    """The mixed box, which is the real one: cursor-lin.yaml declares name: cursor-lin
    (filename == mesh name) and lab.yaml does not."""
    _box(tmp_path, monkeypatch, {
        "lab.yaml": "name: lab-ovh   # mesh peer name\nrole: lab\n",
        "cursor-lin.yaml": "name: cursor-lin\nrole: cursor-lin\n",
    })

    assert gh_route._cells_on_this_box() == ["cursor-lin", "lab-ovh"]


def test_the_FALSE_NEGATIVE_direction_a_filename_key_must_NOT_satisfy_doctor(
        tmp_path, monkeypatch, capsys):
    """>>> THE DANGEROUS HALF. <<< Mapping the FILENAME ('lab') must not turn doctor
    green, because the router will still look up the mesh name ('lab-ovh') and refuse.
    Before the fix this produced a green preflight for an install that refuses every
    gh call — the exact false-green the doctor exists to prevent."""
    _box(tmp_path, monkeypatch, {"lab.yaml": "name: lab-ovh\nrole: lab\n"})
    m = tmp_path / "gh-identities.json"
    m.write_text(json.dumps({"lab": "some-login"}))          # the WRONG key
    monkeypatch.setattr(gh_route.ghid, "mapping_path", lambda: m)

    rc = gh_route.run_doctor()

    out = capsys.readouterr().out
    assert rc == 1, "a filename-keyed mapping must NOT certify the install"
    assert "lab-ovh" in out and "(unmapped)" in out


# ── gpu-wsl's review findings on PR #262 ────────────────────────────────────

def test_a_QUOTED_name_containing_a_hash_is_not_truncated(tmp_path, monkeypatch):
    """gpu-wsl, reviewing #262: the first version stripped inline comments with an
    unconditional split('#',1), which does not respect quoting — `name: "cell#7"`
    truncated to 'cell'. A real parser costs nothing here: cell.py:254 already does a
    LOCAL `import yaml` for this exact file format, specifically to keep
    `swarph --version` PyYAML-free."""
    _box(tmp_path, monkeypatch, {"q.yaml": 'name: "cell#7"\nrole: q\n'})

    assert gh_route._cells_on_this_box() == ["cell#7"]


def test_an_UNREADABLE_config_is_NOT_reported_as_a_cell_named_after_its_file(
        tmp_path, monkeypatch, capsys):
    """>>> THE DOCSTRING PROMISED THIS AND THE CODE DID NOT DO IT. <<< gpu-wsl: OSError
    and no-name-found both set name=None and both fell through to f.stem identically,
    with no test either direction — so an unreadable config was silently reported as a
    mesh name nothing had asserted. That is the invents-a-fact failure the same
    docstring warns against.

    Coverage that cannot be established must not be reported as coverage that was."""
    d = _box(tmp_path, monkeypatch, {"broken.yaml": "name: fine\n"})
    (d / "broken.yaml").chmod(0o000)
    try:
        out = gh_route._cells_on_this_box()
    finally:
        (d / "broken.yaml").chmod(0o644)

    assert out == [], f"an unreadable config must not become a mesh name — got {out}"
    assert "UNREADABLE" in capsys.readouterr().err


def test_a_readable_config_with_NO_name_still_falls_back_to_the_stem(tmp_path, monkeypatch):
    """NON-VACUITY for the distinction above. The fallback must survive for the case it
    was written for — a config that PARSED and simply declares no name is a real fact
    about a readable file, unlike a guess about an unreadable one."""
    _box(tmp_path, monkeypatch, {"nameless.yaml": "role: whatever\n"})

    assert gh_route._cells_on_this_box() == ["nameless"]
