"""card #1396 (#559 phase 2): `board cards dev-extract` — pinned codegraph +
federation extract for spec/plan cards.

Fixtures use the REAL index.db table shapes (repos/symbols/edges CREATE TABLE
statements copied from the live schema) in temp sqlite dbs, plus a temp
federation report + structurizr workspace. The live sources are never touched.
"""
import json
import os
import sqlite3

import pytest

from swarph_cli.commands import dev_extract as de

REPOS_DDL = (
    "CREATE TABLE repos(name TEXT PRIMARY KEY, slug TEXT, path TEXT,"
    " visibility TEXT, indexed_at TEXT, mode TEXT)"
)
SYMBOLS_DDL = (
    "CREATE TABLE symbols(id INTEGER PRIMARY KEY, repo TEXT, name TEXT,"
    " kind TEXT, file_path TEXT, start_line INTEGER,"
    " qualified_name TEXT, docstring TEXT, signature TEXT, name_search TEXT)"
)
EDGES_DDL = (
    "CREATE TABLE edges(src_symbol INTEGER, dst_symbol INTEGER,"
    " edge_type TEXT, repo TEXT)"
)


@pytest.fixture()
def idx(tmp_path):
    p = tmp_path / "index.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute(
        "INSERT INTO repos VALUES (?,?,?,?,?,?)",
        ("swarph-cli", "BrainSurfing-tech/swarph-cli", str(tmp_path / "cli"),
         "public", "2026-11-03T00:31:24+00:00", "full"),
    )
    con.execute(
        "INSERT INTO repos VALUES (?,?,?,?,?,?)",
        ("mesh-gateway", "darw007d/mesh-gateway", str(tmp_path / "gw"),
         "private", "2026-11-03T00:31:37+00:00", "full"),
    )
    con.execute(
        "INSERT INTO repos VALUES (?,?,?,?,?,?)",
        ("mystery", "o/mystery", str(tmp_path / "m"), None,
         "2026-11-03T00:31:37+00:00", "full"),
    )
    # public target with one caller + a test + a route
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, "swarph-cli", "chat_sync", "function",
         "src/swarph_cli/gateway/server.py", 3950, "chat_sync", "", "def chat_sync():", ""),
    )
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (2, "swarph-cli", "board_cards", "function",
         "src/swarph_cli/commands/board.py", 10, "board_cards", "", "", ""),
    )
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (3, "swarph-cli", "POST /api/chat-sync", "route",
         "src/swarph_cli/gateway/server.py", 3949, "POST /api/chat-sync", "", "", ""),
    )
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (4, "swarph-cli", "test_chat_sync_requires_auth", "function",
         "tests/test_server.py", 1594, "test_chat_sync_requires_auth", "", "", ""),
    )
    # same-named symbol in the PRIVATE repo: must never surface
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (5, "mesh-gateway", "chat_sync", "function",
         "server.py", 12, "chat_sync", "", "SECRET-SIG", ""),
    )
    # unknown-visibility repo symbol: fail closed too
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (6, "mystery", "chat_sync", "function", "m.py", 1, "chat_sync", "", "", ""),
    )
    con.execute(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
        (7, "swarph-cli", "test_server.py", "file",
         "tests/test_server.py", 1, "tests/test_server.py", "", "", ""),
    )
    con.execute("INSERT INTO edges VALUES (?,?,?,?)", (2, 1, "calls", "swarph-cli"))
    con.commit()
    con.close()
    (tmp_path / "cli" / ".codegraph").mkdir(parents=True)
    (tmp_path / "cli" / ".codegraph" / "indexed_origin_main").write_text(
        "5e4283274c3638a78dd4736bcd0a8894616b43b1\n")
    return p


@pytest.fixture()
def fed(tmp_path):
    rep = tmp_path / "federation-report.txt"
    # utf-8 pinned: the ──▶ glyphs break the Windows cp1252 default codec.
    rep.write_text(
        "# swarph federation — cross-repo connection map\n"
        "# generated 2026-11-02T04:45:01Z by fed_refresh.sh (card #44 part 4)\n"
        "\n"
        "  Layer 2:\n"
        "    • swarph-cli   [10720 nodes/26662 edges]  ──depends on──▶ swarph-mesh\n"
        "        ↳ imports swarph-mesh: 1 site(s) — e.g. swarph_mesh @ src/x.py:39\n"
        "  • swarph-cli       → (nothing depends on it)\n",
        encoding="utf-8",
    )
    # Real workspace.json shape: ONE system ("Swarph Federation") holding a
    # container per repo, `group` = deploy host.
    ws = {
        "model": {"softwareSystems": [
            {"name": "Swarph Federation",
             "containers": [
                 {"id": "8", "name": "swarph-cli", "group": "lab-ovh",
                  "relationships": [{"destinationId": "7",
                                     "description": "imports"}]},
                 {"id": "7", "name": "swarph-mesh", "group": "lab-ovh",
                  "relationships": []},
             ]},
        ]}
    }
    wsp = tmp_path / "workspace.json"
    wsp.write_text(json.dumps(ws))
    return rep, wsp


def _build(idx, fed, tokens, **kw):
    rep, wsp = fed
    kw.setdefault("merged_lookup", lambda path: None)
    return de.build_extract(tokens=tokens, index_path=str(idx),
                            fed_report=str(rep), structurizr_ws=str(wsp),
                            fed_docs_db=str(rep) + ".nodb", **kw)


def test_public_symbol_resolves_with_file_line_and_pin(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    assert ex["index"]["status"] == "ok"
    rows = [s for s in ex["symbols"] if s["repo"] == "swarph-cli"
            and s["name"] == "chat_sync"]
    assert len(rows) == 1
    assert (rows[0]["file_path"], rows[0]["start_line"]) == \
        ("src/swarph_cli/gateway/server.py", 3950)
    assert ex["pins"]["swarph-cli"]["sha"] == \
        "5e4283274c3638a78dd4736bcd0a8894616b43b1"
    assert ex["pins"]["swarph-cli"]["indexed_at"].startswith("2026-11-03")


def test_private_repo_symbols_withheld_and_named(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    assert "mesh-gateway" in ex["withheld"]
    assert "mystery" in ex["withheld"]  # unknown visibility fails closed too
    leaked = [s for s in ex["symbols"] if s["repo"] in ("mesh-gateway", "mystery")]
    assert leaked == []
    text = de.render(ex)
    assert "WITHHELD: mesh-gateway" in text
    assert "SECRET-SIG" not in text  # no private detail leaks into the extract


def test_zero_callers_prints_floor_verbatim(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    lone = [s for s in ex["symbols"] if s["name"] == "POST /api/chat-sync"]
    assert lone == []  # route rows live in the routes section, not symbols
    ex2 = _build(idx, fed, ["board_cards"])
    text = de.render(ex2)
    assert "0 callers (floor: HTTP callers not linked yet)" in text


def test_zero_routes_prints_floor_and_removed_sections_absent(idx, fed):
    # #1410: routes floor stays; the covering-tests section and every
    # downstream/rels line are gone from the output entirely.
    ex = _build(idx, fed, ["board_cards"])
    text = de.render(ex)
    assert "0 routes (floor: route registry not linked for this symbol)" in text
    assert "covering tests" not in text.lower()
    assert "downstream" not in text.lower()


def test_missing_index_prints_unavailable_never_zero(tmp_path, fed):
    rep, wsp = fed
    ex = de.build_extract(tokens=["x"], index_path=str(tmp_path / "no.db"),
                          fed_report=str(rep), structurizr_ws=str(wsp))
    assert "unavailable" in ex["index"]["status"]
    assert " 0 " not in de.render(ex).split("targets:")[0]


def test_structurizr_container_match_names_deploy_unit(idx, fed):
    # #1410: deploy units stay; downstream rels are never read nor printed.
    ex = _build(idx, fed, ["chat_sync"])
    assert ex["structurizr"]["status"] == "ok"
    assert ex["structurizr"]["deploy_units"] == ["lab-ovh"]
    assert "downstream" not in ex["structurizr"]
    text = de.render(ex)
    assert "deploy units: lab-ovh" in text
    assert "downstream" not in text.lower()


def test_no_covering_tests_section(idx, fed):
    # #1410: the covering-tests section is gone — no `tests` key, no tests
    # text in the output — while the symbol itself still resolves.
    ex = _build(idx, fed, ["chat_sync"])
    assert "tests" not in ex
    rows = [s for s in ex["symbols"] if s["name"] == "chat_sync"]
    assert len(rows) == 1
    text = de.render(ex)
    assert "covering tests" not in text.lower()
    ex2 = _build(idx, fed, ["board_cards"])
    assert "tests" not in ex2
    assert "covering tests" not in de.render(ex2).lower()


def test_missing_federation_sources_print_unavailable(tmp_path, idx):
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(idx),
                          fed_report=str(tmp_path / "no-report.txt"),
                          structurizr_ws=str(tmp_path / "no-ws.json"),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    text = de.render(ex)
    assert "federation-report: unavailable" in text
    assert "structurizr: unavailable" in text
    assert "fed-docs.db: unavailable" in text


def test_stale_when_fed_build_older_than_merged(idx, fed):
    rep, wsp = fed
    os.utime(str(rep), (1762046701, 1762046701))  # 2026-11-02T04:45:01Z
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(idx),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db="x",
                          merged_lookup=lambda p: "2026-11-02T17:13:17+00:00")
    text = de.render(ex)
    assert "STALE (federation build" in text
    assert "last repo.merged" in text


def test_fresh_when_fed_build_newer_than_merged(idx, fed):
    ex = _build(idx, fed, ["chat_sync"],
                merged_lookup=lambda p: "2026-01-01T00:00:00+00:00")
    assert "fresh (federation build" in de.render(ex)


def test_unknown_merged_prints_unavailable_freshness(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])  # merged_lookup -> None
    text = de.render(ex)
    assert "freshness unavailable" in text
    assert "STALE (federation build" not in text


def test_output_cap_states_what_was_cut(idx, fed):
    ex = _build(idx, fed, ["chat_sync", "board_cards"])
    text = de.render(ex, max_lines=10)
    assert len(text.splitlines()) <= 10
    assert "cut:" in text


def test_every_row_query_is_bounded():
    import re
    src = open(de.__file__, encoding="utf-8").read()
    # Each con.execute( call's full statement (queries span several literals)
    # must bound rows: an explicit LIMIT, or a COUNT(*) aggregate which
    # materializes no rows.
    for m in re.finditer(r"con\.execute\(", src):
        stmt = src[m.start():m.start() + 900]
        assert ("LIMIT" in stmt.upper() or "COUNT(*)" in stmt), \
            f"unbounded row query near: {stmt[:120]}"


def test_verb_wired_under_board_cards():
    from swarph_cli.commands import board as board_mod
    args = board_mod._build_parser().parse_args(
        ["cards", "dev-extract", "--card", "47", "--print-only"])
    assert (args.group, args.command, args.card) == ("cards", "dev-extract", 47)


def test_extract_card_tokens_capped_and_ordered():
    card = {"title": "use `chat_sync` here",
            "body": "also run_triage and POST /api/chat-sync plus extra"}
    toks = de.extract_card_tokens(card)
    assert toks[0] == "chat_sync"  # backtick spans first
    assert "run_triage" in toks
    assert len(toks) <= de.MAX_AUTO_TOKENS
    assert de.extract_card_tokens({"title": "t " + "x_y " * 50, "body": ""}) \
        == ["x_y"]


# ── rework r1: same-named symbol in a WITHHELD repo is named, never detailed ──

def test_withheld_same_name_named_without_detail(idx, fed):
    # Fixture: `chat_sync` lives in public swarph-cli AND private
    # mesh-gateway (SECRET-SIG) AND unknown-visibility mystery — the exact
    # shape of the reviewer's drop-on-meta-edge complaint.
    ex = _build(idx, fed, ["chat_sync"])
    assert ex["withheld_hits"].get("chat_sync") == {"mesh-gateway": False,
                                                        "mystery": False}
    text = de.render(ex)
    # Visible copy printed in full ...
    assert "src/swarph_cli/gateway/server.py:3950" in text
    # ... withheld twin NAMED with the honest line ...
    assert ("- chat_sync: also defined in withheld mesh-gateway; "
            "the deployed copy may differ") in text
    assert ("- chat_sync: also defined in withheld mystery; "
            "the deployed copy may differ") in text
    # ... and no private detail anywhere: every mesh-gateway/mystery line is
    # the WITHHELD notice, the also-defined line, or the r1 coverage line
    # (which names repo names + card #1404 only — no paths, no symbols).
    for line in text.splitlines():
        if "mesh-gateway" in line or "mystery" in line:
            assert ("WITHHELD" in line or "also defined in withheld" in line
                    or line.startswith("index coverage is partial:")), line
    assert "SECRET-SIG" not in text
    assert "server.py:12" not in text  # the withheld file:line
    assert "m.py:1" not in text  # the unknown-visibility file:line


# ── rework r2: no resolvable repo → honest federation line, no default repo ──

def test_no_visible_target_repo_prints_honest_line(idx, fed):
    ex = _build(idx, fed, ["zz_no_such_symbol_anywhere"])
    assert ex["repos_hit"] == []
    text = de.render(ex)
    assert "federation: no visible target repo" in text
    assert "deploy units:" not in text
    assert "lab-ovh" not in text
    assert "downstream" not in text
    assert "──depends on──▶" not in text


# ── rework r3: mined card text ending in '/' must never traceback ──

def test_trailing_slash_token_never_tracebacks(idx, fed):
    for tok in ("POST /api/chat-sync/", "/api/chat-sync/", "/", ""):
        ex = _build(idx, fed, [tok])  # raised IndexError before the fix
        assert isinstance(ex, dict)
        assert isinstance(de.render(ex), str)


def test_card_body_trailing_slash_print_only_exits_zero(idx, fed, monkeypatch,
                                                        capsys):
    """Through the real board.py branch: mined 'POST /api/chat-sync/' token."""
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    card = {"id": 99, "title": "chat route",
            "body": "fix POST /api/chat-sync/ before release"}
    monkeypatch.setattr(board_mod, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr(board_mod, "_http_get_json",
                        lambda url, tok, **k: (200, card))
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "99", "--print-only",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    assert "Traceback" not in out.out and "Traceback" not in out.err
    assert ("routes + params:" in out.out
            or "no visible target repo" in out.out
            or "no symbol resolved" in out.out)


# ── rework r4: STALE compares instants, not strings, across offsets ──

def _build_at_fed_mtime(idx, fed, tokens, mtime, merged):
    rep, wsp = fed
    os.utime(str(rep), (mtime, mtime))
    return de.build_extract(tokens=tokens, index_path=str(idx),
                            fed_report=str(rep), structurizr_ws=str(wsp),
                            fed_docs_db="x", merged_lookup=lambda p: merged)


def test_stale_plus0200_merged_before_build_is_fresh(idx, fed):
    # 05:30+02:00 == 03:30Z < 04:45Z build → fresh. String compare reads
    # "04:45..." < "05:30..." and wrongly reports STALE.
    ex = _build_at_fed_mtime(idx, fed, ["chat_sync"], 1793594701,
                             "2026-11-02T05:30:00+02:00")
    assert "fresh (federation build" in de.render(ex)
    assert "STALE (federation build" not in de.render(ex)


def test_stale_minus0500_merged_after_build_is_stale(idx, fed):
    # 04:00-05:00 == 09:00Z > 04:45Z build → STALE. String compare reads
    # "04:45..." > "04:00..." and wrongly reports fresh.
    ex = _build_at_fed_mtime(idx, fed, ["chat_sync"], 1793594701,
                             "2026-11-02T04:00:00-05:00")
    text = de.render(ex)
    assert "STALE (federation build" in text
    assert "last repo.merged" in text


def test_stale_unparseable_time_is_unavailable_not_stale(idx, fed):
    ex = _build(idx, fed, ["chat_sync"], merged_lookup=lambda p: "not-a-time")
    text = de.render(ex)
    assert "freshness unavailable (unparseable" in text
    assert "STALE (federation build" not in text


# ── rework r5: --path matches file_path; --caller-cell gone; help is honest ──

def test_path_matches_file_path_and_line(idx, fed):
    ex = de.build_extract(tokens=[], paths=["src/swarph_cli/gateway/server.py"],
                          index_path=str(idx),
                          fed_report=str(fed[0]), structurizr_ws=str(fed[1]),
                          fed_docs_db="x", merged_lookup=lambda p: None)
    hit = [s for s in ex["symbols"] if s["name"] == "chat_sync"]
    assert hit and hit[0]["file_path"] == "src/swarph_cli/gateway/server.py"
    assert hit[0]["start_line"] == 3950
    assert ex["repos_hit"] == ["swarph-cli"]
    # path:line narrows; a wrong line resolves nothing from that file.
    ex2 = de.build_extract(
        tokens=[], paths=["src/swarph_cli/gateway/server.py:3950"],
        index_path=str(idx), fed_report=str(fed[0]),
        structurizr_ws=str(fed[1]), fed_docs_db="x",
        merged_lookup=lambda p: None)
    assert any(s["name"] == "chat_sync" for s in ex2["symbols"])
    ex3 = de.build_extract(
        tokens=[], paths=["src/swarph_cli/gateway/server.py:1"],
        index_path=str(idx), fed_report=str(fed[0]),
        structurizr_ws=str(fed[1]), fed_docs_db="x",
        merged_lookup=lambda p: None)
    assert not [s for s in ex3["symbols"]
                if s["file_path"] == "src/swarph_cli/gateway/server.py"]


def test_path_never_matches_symbol_names_and_withholds_private(idx, fed,
                                                               tmp_path):
    # "server.py" as a NAME matches nothing (lookup is file_path only), while
    # as a path it resolves the visible copy and still withholds mesh-gateway.
    con_idx = tmp_path / "names.db"
    con = sqlite3.connect(str(con_idx))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path), "public", "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "server.py", "file", "x.py", 1, "", "", "", ""))
    con.commit()
    con.close()
    rep, wsp = fed
    ex = de.build_extract(tokens=["x.py"], paths=[], index_path=str(con_idx),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db="x", merged_lookup=lambda p: None)
    assert ex["symbols"] == []  # name lookup is exact-name, not file stem
    # ... while as a PATH it resolves the visible copy and still withholds
    # the private repo's same-named file.
    ex2 = de.build_extract(tokens=[], paths=["server.py"], index_path=str(idx),
                           fed_report=str(rep), structurizr_ws=str(wsp),
                           fed_docs_db="x", merged_lookup=lambda p: None)
    assert any(s["repo"] == "swarph-cli"
               and s["file_path"].endswith("gateway/server.py")
               for s in ex2["symbols"])
    assert [s for s in ex2["symbols"] if s["repo"] == "mesh-gateway"] == []


def test_caller_cell_removed_and_help_states_posting():
    args = de.build_parser().parse_args(["--print-only"])
    assert not hasattr(args, "caller_cell")
    desc = de.build_parser().description
    assert "with --card and without --print-only it posts the extract" in desc
    assert "Dry-run by default" not in desc


# ── #3757(a): unreadable index prints unavailable, never ok-with-zeros ──

def _unavailable_ex(tmp_path, fed, index_path):
    rep, wsp = fed
    return de.build_extract(tokens=["chat_sync"], index_path=str(index_path),
                            fed_report=str(rep), structurizr_ws=str(wsp),
                            fed_docs_db=str(tmp_path / "no-fed.db"),
                            merged_lookup=lambda p: None)


def _assert_unavailable_index(ex):
    assert "unavailable" in ex["index"]["status"]
    text = de.render(ex)
    assert "index unavailable" in text
    assert "codegraph half: unavailable" not in text
    assert "## codegraph half (index.db" not in text
    as_json = json.loads(json.dumps(ex, default=str))
    assert "unavailable" in as_json["index"]["status"]


def test_zero_byte_index_prints_unavailable(tmp_path, fed):
    p = tmp_path / "empty.db"
    p.write_bytes(b"")
    _assert_unavailable_index(_unavailable_ex(tmp_path, fed, p))


def test_garbage_bytes_index_prints_unavailable(tmp_path, fed):
    p = tmp_path / "garbage.db"
    p.write_bytes(b"\x00\x01definitely not a sqlite db\xff" * 16)
    _assert_unavailable_index(_unavailable_ex(tmp_path, fed, p))


def test_schemaless_sqlite_index_prints_unavailable(tmp_path, fed):
    p = tmp_path / "other.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE other(x TEXT)")
    con.commit()
    con.close()
    _assert_unavailable_index(_unavailable_ex(tmp_path, fed, p))


# ── #3757(b): federation half never names a WITHHELD repo ──

def test_federation_half_drops_lines_naming_withheld_repo(idx, tmp_path):
    rep = tmp_path / "fed.txt"
    rep.write_text(
        "# swarph federation — cross-repo connection map\n"
        "# generated 2026-11-02T04:45:01Z by fed_refresh.sh (card #44 part 4)\n"
        "\n"
        "  Layer 2:\n"
        "    • swarph-cli   [10720 nodes/26662 edges]  ──depends on──▶ mesh-gateway\n"
        "    • swarph-cli       → (nothing depends on it)\n",
        encoding="utf-8",
    )
    ws = {"model": {"softwareSystems": [
        {"name": "Swarph Federation", "containers": [
            {"id": "8", "name": "swarph-cli", "group": "lab-ovh",
             "relationships": [{"destinationId": "7",
                                "description": "imports"},
                               {"destinationId": "9",
                                "description": "calls"}]},
            {"id": "7", "name": "mesh-gateway", "group": "lab-ovh",
             "relationships": []},
            {"id": "9", "name": "swarph-mesh", "group": "lab-ovh",
             "relationships": []},
        ]},
    ]}}
    wsp = tmp_path / "ws.json"
    wsp.write_text(json.dumps(ws))
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(idx),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    # mixed fed-report line dropped fail-closed; pure-visible line kept.
    assert ex["federation_report"]["status"] == "ok"
    assert ex["federation_report"]["hits"] == \
        ["• swarph-cli       → (nothing depends on it)"]
    # #1410: rels are never read nor printed — no downstream key at all —
    # while the deploy unit stays.
    assert "downstream" not in ex["structurizr"]
    assert ex["structurizr"]["deploy_units"] == ["lab-ovh"]
    text = de.render(ex)
    # #3779 r4 old->new: WITHHELD moved from five per-repo lines ABOVE the
    # federation header to ONE reserved names-list line in the tail (after
    # the federation middle), so the leak check scopes to the fed lines
    # themselves — the notice MUST name mesh-gateway (asserted elsewhere).
    fed_lines = [ln for ln in text.splitlines()
                 if ln.startswith("- •")
                 or ln.startswith("deploy units:")]
    assert any("nothing depends on it" in ln for ln in fed_lines)
    assert any("swarph-cli" in ln for ln in fed_lines)  # visible kept
    for ln in fed_lines:
        assert "mesh-gateway" not in ln, ln  # no private name via fed lines
        assert "mystery" not in ln, ln
    assert "swarph-cli" in text.split("## federation half")[1]
    assert "downstream" not in text.lower()


# ── #3757(c): --json redacts WITHHELD registry entries ──

def test_json_redacts_withheld_registry_paths(idx, fed, monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    monkeypatch.setattr(board_mod, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_token", lambda *_a, **_k: "tok")
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--symbol", "chat_sync", "--json",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    priv = payload["registry"]["mesh-gateway"]
    assert set(priv) == {"name", "visibility", "reason"}
    assert "path" not in json.dumps(payload["registry"]["mesh-gateway"])
    assert payload["registry"]["mesh-gateway"]["visibility"] == "private"
    assert set(payload["registry"]["mystery"]) == {"name", "visibility", "reason"}
    assert str(idx.parent / "gw") not in out.out  # no withheld path leaks
    assert "2026-11-03T00:31:37" not in out.out  # no withheld indexed_at leaks
    # visible pins survive redaction.
    assert payload["registry"]["swarph-cli"]["path"] == str(idx.parent / "cli")
    assert payload["pins"]["swarph-cli"]["sha"] == \
        "5e4283274c3638a78dd4736bcd0a8894616b43b1"


def test_redact_for_json_does_not_mutate_input(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    before = json.dumps(ex, default=str)
    red = de.redact_for_json(ex)
    assert json.dumps(ex, default=str) == before  # input untouched
    assert red["registry"]["mesh-gateway"].get("path") is None
    assert red["registry"]["swarph-cli"]["path"] is not None
    assert de.render(ex).find("WITHHELD: mesh-gateway") != -1  # text intact


# ── #3757(d): fed-docs.db wording claims absence only when absent ──

def test_fed_docs_present_but_empty_says_present_not_absent(idx, fed, tmp_path):
    present = tmp_path / "fed-docs.db"
    present.write_bytes(b"")  # present but empty
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(idx),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(present),
                          merged_lookup=lambda p: None)
    text = de.render(ex)
    assert f"present ({present})" in text
    assert "not linked yet" in text
    assert "not present on this box" not in text
    assert "absent on this box" not in text


# ── #3776 r1: coverage line in every index-ok extract (one line) ──

def _coverage_lines(text):
    return [ln for ln in text.splitlines()
            if ln.startswith("index coverage is partial:")]


def test_coverage_line_prints_with_symbols_found(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    cov = _coverage_lines(de.render(ex))
    assert len(cov) == 1  # ONE line — the render cap counts lines
    line = cov[0]
    assert "0 rows for a whole main module" in line  # index can miss files
    assert "card #1404" in line
    assert "mesh-gateway" in line and "mystery" in line  # repo names only
    assert ("a visible match is the indexed copy, "
            "not evidence the deployed code is here") in line
    # no withheld detail: no file paths, no symbol names of withheld repos.
    assert "server.py" not in line
    assert "SECRET-SIG" not in line
    assert "chat_sync" not in line


def test_coverage_line_prints_with_no_symbols_found(idx, fed):
    ex = _build(idx, fed, ["zz_no_such_symbol_anywhere"])
    cov = _coverage_lines(de.render(ex))
    assert len(cov) == 1
    assert "card #1404" in cov[0]
    assert "indexed copy" in cov[0]


def test_coverage_line_absent_when_index_unavailable(tmp_path, fed):
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(tmp_path / "no.db"),
                          fed_report=str(rep), structurizr_ws=str(wsp))
    text = de.render(ex)
    assert "index unavailable" in text
    assert "codegraph half: unavailable" not in text
    assert "index coverage is partial:" not in text


# ── #3776 r2: swarph-cli gateway-copy note (that path shape only) ──

_GW_LINE = ("swarph-cli gateway copy; "
            "the deployed gateway is a withheld repo")


def test_gateway_copy_note_prints_for_gateway_path(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])  # src/swarph_cli/gateway/server.py
    assert _GW_LINE in de.render(ex)


def test_gateway_copy_note_absent_for_other_paths(idx, fed):
    ex = _build(idx, fed, ["board_cards"])  # .../commands/board.py, not gateway
    text = de.render(ex)
    assert "index coverage is partial:" in text  # r1 still prints
    assert "gateway copy" not in text


def test_gateway_copy_note_absent_when_gateway_visible(idx, fed):
    # #1410: the note warns the deployed gateway is WITHHELD — on a card
    # whose project allowlists mesh-gateway the deployed copy prints in
    # full beside it, so the warning would be false and must not print.
    ex = _allow_build(idx, fed, ["chat_sync"], "board", _ALLOW)
    assert any(s["repo"] == "mesh-gateway" for s in ex["symbols"])
    assert "gateway copy" not in de.render(ex)


# ── #3776 r3: withheld test-helper rows worded as test code ──

def _test_shape_db(tmp_path, withheld_rows):
    p = tmp_path / "t.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path / "cli"),
                 "public", "t", "full"))
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("mesh-gateway", "o/gw", str(tmp_path / "gw"),
                 "private", "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "chat_sync", "function",
                 "src/swarph_cli/gateway/server.py", 3950, "", "", "", ""))
    for i, fp in enumerate(withheld_rows, start=2):
        con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (i, "mesh-gateway", "chat_sync", "function",
                     fp, 7, "", "", "", ""))
    con.commit()
    con.close()
    return p


def _test_shape_extract(tmp_path, fed, withheld_rows):
    p = _test_shape_db(tmp_path, withheld_rows)
    rep, wsp = fed
    return de.build_extract(tokens=["chat_sync"], index_path=str(p),
                            fed_report=str(rep), structurizr_ws=str(wsp),
                            fed_docs_db=str(tmp_path / "no-fed.db"),
                            merged_lookup=lambda path: None)


def test_withheld_test_helper_worded_as_test_code(tmp_path, fed):
    # The withheld twin exists ONLY under tests/ (a test helper): the line
    # must say test code, not imply a deployed copy that differs.
    ex = _test_shape_extract(tmp_path, fed, ["tests/test_server.py"])
    assert ex["withheld_hits"].get("chat_sync") == {"mesh-gateway": True}
    text = de.render(ex)
    assert ("- chat_sync: also defined in withheld mesh-gateway (test code); "
            "the deployed copy may differ") in text
    assert "- chat_sync: also defined in withheld mesh-gateway;" not in text


def test_withheld_mixed_test_and_real_rows_uses_plain_wording(tmp_path, fed):
    # A real (deployed-shape) row exists alongside the tests/ twin, so the
    # plain line holds — the (test code) qualifier is for test-only repos.
    ex = _test_shape_extract(tmp_path, fed,
                             ["tests/test_server.py", "server.py"])
    assert ex["withheld_hits"].get("chat_sync") == {"mesh-gateway": False}
    text = de.render(ex)
    assert ("- chat_sync: also defined in withheld mesh-gateway; "
            "the deployed copy may differ") in text
    assert "(test code)" not in text


# ── #3779 r1: kind=import rows never resolve ──

def _import_shape_db(tmp_path):
    """swarph_cli lives ONLY as import rows; one route row carries the token
    in its path-embedding qualified_name; delivery's test file mixes a real
    test with import rows (sys/json)."""
    p = tmp_path / "imp.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path / "cli"),
                 "public", "t", "full"))
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("mesh-gateway", "o/gw", str(tmp_path / "gw"),
                 "private", "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "swarph_cli", "import",
                 "src/swarph_cli/channel.py", 16, "swarph_cli",
                 "", "from swarph_cli import identity", ""))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (2, "swarph-cli", "POST /api/chat-sync", "route",
                 "src/swarph_cli/gateway/server.py", 3949,
                 "src/swarph_cli/gateway/server.py::POST:/api/chat-sync",
                 "", "", ""))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (3, "swarph-cli", "test_delivery_ok", "function",
                 "tests/test_thing_delivery.py", 5, "test_delivery_ok",
                 "", "", ""))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (4, "swarph-cli", "sys", "import",
                 "tests/test_thing_delivery.py", 1, "sys", "", "import sys", ""))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (5, "swarph-cli", "json", "import",
                 "tests/test_thing_delivery.py", 2, "json", "", "import json", ""))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (6, "mesh-gateway", "swarph_cli", "import",
                 "server.py", 3, "swarph_cli", "", "import swarph_cli", ""))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (7, "mesh-gateway", "chat_sync", "function",
                 "server.py", 12, "chat_sync", "", "", ""))
    con.commit()
    con.close()
    return p


def _import_shape_extract(tmp_path, fed, tokens):
    p = _import_shape_db(tmp_path)
    rep, wsp = fed
    return de.build_extract(tokens=tokens, index_path=str(p),
                            fed_report=str(rep), structurizr_ws=str(wsp),
                            fed_docs_db=str(tmp_path / "no-fed.db"),
                            merged_lookup=lambda path: None)


def test_import_rows_never_resolve_as_symbols(tmp_path, fed):
    # card #792: `swarph_cli` resolved to import sites, not the symbol.
    ex = _import_shape_extract(tmp_path, fed, ["swarph_cli"])
    assert ex["symbols"] == []
    assert "no symbol resolved" in de.render(ex)


def test_import_rows_excluded_from_covering_tests(tmp_path, fed):
    # card #788 shape, #1410 contract: a stem-like token resolves nothing
    # (exact-name only — `delivery` is not a symbol here) and the extract
    # carries no covering-tests at all, so no import row can pose as one.
    ex = _import_shape_extract(tmp_path, fed, ["delivery"])
    assert ex["symbols"] == []
    assert "tests" not in ex
    assert "covering tests" not in de.render(ex).lower()


def test_import_rows_excluded_from_also_defined(tmp_path, fed):
    p = _import_shape_db(tmp_path)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        reg = de.repo_registry(con)
        vis, withheld = de.split_visible(reg)
        assert de.withheld_symbol_repos(con, "swarph_cli", withheld) == {}
        assert de.withheld_symbol_repos(con, "chat_sync", withheld) == \
            {"mesh-gateway": False}
    finally:
        con.close()


def test_path_lookup_excludes_import_rows(tmp_path, fed):
    p = _import_shape_db(tmp_path)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        reg = de.repo_registry(con)
        vis, _ = de.split_visible(reg)
        assert de.find_symbols_by_path(con, "src/swarph_cli/channel.py",
                                       vis) == []
    finally:
        con.close()


def test_routes_match_route_name_not_file_path(tmp_path, fed):
    # card #792: token `swarph_cli` matched all 76 route qualified_names
    # via the path-embedding `src/swarph_cli/...` prefix.
    p = _import_shape_db(tmp_path)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        reg = de.repo_registry(con)
        vis, _ = de.split_visible(reg)
        assert de.find_routes(con, "swarph_cli", vis) == []
        hit = de.find_routes(con, "chat_sync", vis)
        assert [r["name"] for r in hit] == ["POST /api/chat-sync"]
        # mined card text with a trailing slash still hits the route row.
        hit2 = de.find_routes(con, "POST /api/chat-sync/", vis)
        assert [r["name"] for r in hit2] == ["POST /api/chat-sync"]
        assert de.find_routes(con, "/", vis) == []
    finally:
        con.close()


# ── #3779 r2: junk targets dropped before lookup ──

def test_is_lookup_token_predicate():
    for junk in ("?", "none", "NONE", "pip install -U",
                 "python3 -m swarph_cli.scripts.dm_notify_filter",
                 "swarph version", "import json, sys", "cards graph",
                 "delivery = sd.get(\"delivery", "json.loads(row[\"menu_json",
                 "", "x"):
        assert not de.is_lookup_token(junk), junk
    for keep in ("chat_sync", "delivery", "_check_delivery_container",
                 "_step_menu_for", "dm_notify_filter", "swarph_cli",
                 "POST /api/chat-sync", "POST /api/chat-sync/",
                 "/api/chat-sync/"):
        assert de.is_lookup_token(keep), keep


def test_mined_card_tokens_skip_junk():
    # card #788/#792 shaped text: backticked junk + shell + real symbols.
    card = {"title": "render `?` for unknown, `none` for unchecked",
            "body": ("run `pip install -U` and `import json, sys` "
                     "(`swarph version`); fix `delivery` via "
                     "`_check_delivery_container`; use POST /api/chat-sync")}
    toks = de.extract_card_tokens(card)
    assert "delivery" in toks
    assert "_check_delivery_container" in toks
    assert "POST /api/chat-sync" in toks
    for junk in ("?", "none", "pip install -U", "import json, sys",
                 "swarph version"):
        assert junk not in toks, junk


def test_build_extract_drops_explicit_junk_tokens(idx, fed):
    ex = _build(idx, fed, ["none", "?", "delivery", "chat_sync"])
    assert ex["tokens"] == ["delivery", "chat_sync"]
    assert "none" not in de.render(ex).split("targets:")[1].splitlines()[0]


# ── #3779 r3: unavailable index keeps true fed-docs; probe covers columns ──

def test_unavailable_index_keeps_true_fed_docs_state(tmp_path, fed):
    rep, wsp = fed
    missing = tmp_path / "no-fed.db"
    ex = de.build_extract(tokens=["chat_sync"],
                          index_path=str(tmp_path / "no.db"),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(missing),
                          merged_lookup=lambda p: None)
    assert ex["fed_docs"].startswith("unavailable")
    assert "not present on this box" in de.render(ex)
    as_json = de.redact_for_json(ex)
    assert as_json["fed_docs"] == ex["fed_docs"]
    present = tmp_path / "fed.db"
    present.write_bytes(b"")
    ex2 = de.build_extract(tokens=["chat_sync"],
                           index_path=str(tmp_path / "no.db"),
                           fed_report=str(rep), structurizr_ws=str(wsp),
                           fed_docs_db=str(present),
                           merged_lookup=lambda p: None)
    assert f"present ({present})" in de.render(ex2)


def _col_drop_db(tmp_path, name, repos_ddl, symbols_ddl,
                 repos_row, symbols_row):
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.execute(f"CREATE TABLE repos({repos_ddl})")
    con.execute(f"CREATE TABLE symbols({symbols_ddl})")
    con.execute(EDGES_DDL)
    con.execute(f"INSERT INTO repos VALUES ({','.join('?' * len(repos_row))})",
                repos_row)
    con.execute(
        f"INSERT INTO symbols VALUES ({','.join('?' * len(symbols_row))})",
        symbols_row)
    con.commit()
    con.close()
    return p


def test_probe_rejects_dropped_signature_column(tmp_path, fed):
    p = _col_drop_db(
        tmp_path, "nosig.db",
        "name TEXT PRIMARY KEY, slug TEXT, path TEXT,"
        " visibility TEXT, indexed_at TEXT, mode TEXT",
        "id INTEGER PRIMARY KEY, repo TEXT, name TEXT,"
        " kind TEXT, file_path TEXT, start_line INTEGER,"
        " qualified_name TEXT, docstring TEXT, name_search TEXT",
        ("swarph-cli", "s", str(tmp_path), "public", "t", "full"),
        (1, "swarph-cli", "chat_sync", "function", "s.py", 1,
         "chat_sync", "", ""))
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(p),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    assert "unavailable" in ex["index"]["status"]
    assert "index unavailable" in de.render(ex)


def test_probe_rejects_dropped_repos_path_column(tmp_path, fed):
    p = _col_drop_db(
        tmp_path, "nopath.db",
        "name TEXT PRIMARY KEY, slug TEXT,"
        " visibility TEXT, indexed_at TEXT, mode TEXT",
        "id INTEGER PRIMARY KEY, repo TEXT, name TEXT,"
        " kind TEXT, file_path TEXT, start_line INTEGER,"
        " qualified_name TEXT, docstring TEXT, signature TEXT,"
        " name_search TEXT",
        ("swarph-cli", "s", "public", "t", "full"),
        (1, "swarph-cli", "chat_sync", "function", "s.py", 1,
         "chat_sync", "", "def chat_sync():", ""))
    rep, wsp = fed
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        assert de.probe_index(con) is not None
    finally:
        con.close()


def test_empty_index_reads_unavailable_not_ok(tmp_path, fed):
    p = tmp_path / "empty.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.commit()
    con.close()
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(p),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    assert "unavailable" in ex["index"]["status"]
    assert "empty index" in ex["index"]["status"]
    text = de.render(ex)
    assert "index unavailable" in text
    assert "no symbol resolved" not in text  # symbol section never renders
    assert "- 0 symbols" not in text


# ── #3779 r4: cap reserves freshness/withheld/cut; WITHHELD is one line ──

def test_withheld_prints_as_one_names_list_line(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    assert ex["withheld"] == ["mesh-gateway", "mystery"]
    cov = [ln for ln in de.render(ex).splitlines()
           if ln.startswith("WITHHELD")]
    assert len(cov) == 1  # ONE line, not five per-repo lines
    assert "mesh-gateway, mystery" in cov[0]
    text = de.render(ex)
    assert "+ 0 more withheld" not in text
    assert "more withheld (full list in --json)" not in text
    assert "withheld list cut after" not in text


def test_output_cap_reserves_freshness_withheld_and_cut(idx, fed):
    # Old code cut from the very end: at max_lines=20 the freshness
    # section (and its STALE line) vanished first.
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync", "board_cards"],
                          index_path=str(idx),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(rep) + ".nodb",
                          merged_lookup=lambda p: "2027-01-01T00:00:00+00:00")
    text = de.render(ex, max_lines=20)
    assert len(text.splitlines()) <= 20
    assert "cut:" in text
    assert "WITHHELD:" in text
    assert "fed-docs.db:" in text
    assert "freshness (STALE when fed build < last repo.merged):" in text
    assert "STALE (federation build" in text


# ── #3779 r5: dotted/slashed private names + unknown registry names ──

def _r5_db(tmp_path):
    p = tmp_path / "r5.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path / "cli"),
                 "public", "t", "full"))
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("proven", "o/proven", str(tmp_path / "pr"),
                 "private", "t", "full"))
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("mesh-gateway", "o/gw", str(tmp_path / "gw"),
                 "private", "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "chat_sync", "function",
                 "src/svc.py", 10, "chat_sync", "", "", ""))
    con.commit()
    con.close()
    return p


def _r5_sources(tmp_path):
    rep = tmp_path / "fed.txt"
    rep.write_text(
        "# swarph federation — cross-repo connection map\n"
        "# generated 2026-11-02T04:45:01Z by fed_refresh.sh (card #44 part 4)\n"
        "\n"
        "  Layer 2:\n"
        "    • swarph-cli       → (nothing depends on it)\n"
        "    • swarph-cli   [10720 nodes/26632 edges]  ──depends on──▶ proven.packs.secret_mod\n"
        "    • swarph-cli   [10720 nodes/26632 edges]  ──depends on──▶ mesh_gateway.private_helper\n"
        "    • swarph-cli   [10720 nodes/26632 edges]  ──depends on──▶ swarph-panel\n"
        "    • swarph-cli   [10720 nodes/26632 edges]  ──depends on──▶ swarph-mesh\n",
        encoding="utf-8",
    )
    ws = {"model": {"softwareSystems": [
        {"name": "Swarph Federation", "containers": [
            {"id": "8", "name": "swarph-cli", "group": "lab-ovh",
             "relationships": [{"destinationId": "7",
                                "description": "imports"},
                               {"destinationId": "9",
                                "description": "calls"},
                               {"destinationId": "phantom-svc",
                                "description": "uses"}]},
            {"id": "7", "name": "mesh-gateway", "group": "lab-ovh",
             "relationships": []},
            {"id": "9", "name": "swarph-mesh", "group": "lab-ovh",
             "relationships": []},
        ]},
    ]}}
    wsp = tmp_path / "ws.json"
    wsp.write_text(json.dumps(ws))
    return rep, wsp


def test_federation_drops_dotted_slashed_and_unknown_names(tmp_path):
    p = _r5_db(tmp_path)
    rep, wsp = _r5_sources(tmp_path)
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(p),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    hits = ex["federation_report"]["hits"]
    # pure-visible lines kept, including the known-container swarph-mesh
    # line (registered as a structurizr container, fail-closed keeps known).
    assert "• swarph-cli       → (nothing depends on it)" in hits
    assert any("swarph-mesh" in h for h in hits)
    for h in hits:
        assert "proven" not in h, h  # dotted private path dropped
        assert "mesh_gateway" not in h, h  # slashed private path dropped
        assert "swarph-panel" not in h, h  # unregistered name dropped
    text = de.render(ex)
    fed_half = text.split("## federation half")[1].split("WITHHELD:")[0]
    assert "secret_mod" not in fed_half
    assert "private_helper" not in fed_half
    assert "swarph-panel" not in fed_half


def test_structurizr_drops_withheld_and_unregistered_rels(tmp_path):
    # #1410: rels are never read nor printed — no downstream key, no rel
    # text — while deploy units and the fed-report filtering stay.
    p = _r5_db(tmp_path)
    rep, wsp = _r5_sources(tmp_path)
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(p),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    assert "downstream" not in ex["structurizr"]
    assert ex["structurizr"]["deploy_units"] == ["lab-ovh"]
    text = de.render(ex)
    assert "downstream" not in text.lower()
    assert "rels" not in text.lower()
    assert "phantom-svc" not in text
    assert "mesh_gateway" not in text.split("WITHHELD:")[0]


# ── #1410: project allowlist visibility (card-allowlist.json) ──

_ALLOW = {"board": ["mesh-gateway", "lab-orchestrator"],
          "other": ["mystery"]}


def _allow_build(idx, fed, tokens, project_slug, allowlist, **kw):
    rep, wsp = fed
    kw.setdefault("merged_lookup", lambda p: None)
    return de.build_extract(tokens=tokens, index_path=str(idx),
                            fed_report=str(rep), structurizr_ws=str(wsp),
                            fed_docs_db=str(rep) + ".nodb",
                            project_slug=project_slug,
                            card_allowlist=allowlist, **kw)


def test_allowlisted_private_repo_visible_on_mapped_project(idx, fed):
    ex = _allow_build(idx, fed, ["chat_sync"], "board", _ALLOW)
    hit = {(s["repo"], s["name"], s["file_path"], s["start_line"])
           for s in ex["symbols"]}
    assert ("mesh-gateway", "chat_sync", "server.py", 12) in hit
    assert ("swarph-cli", "chat_sync",
            "src/swarph_cli/gateway/server.py", 3950) in hit
    assert "mesh-gateway" not in ex["withheld"]
    assert "mystery" in ex["withheld"]  # not mapped to board: stays out
    text = de.render(ex)
    assert "server.py:12" in text  # the private file:line now prints
    assert "SECRET-SIG" in text  # ... with its full detail, like any row
    assert "also defined in withheld mesh-gateway" not in text


def test_repo_mapped_to_another_project_stays_withheld(idx, fed):
    # mesh-gateway is allowlisted for `board`, not for `other`: on an
    # `other` card it must behave exactly like public-only.
    ex = _allow_build(idx, fed, ["chat_sync"], "other",
                      {"board": ["mesh-gateway"]})
    assert [s for s in ex["symbols"] if s["repo"] == "mesh-gateway"] == []
    assert "mesh-gateway" in ex["withheld"]
    text = de.render(ex)
    assert "server.py:12" not in text
    assert "SECRET-SIG" not in text
    assert ("- chat_sync: also defined in withheld mesh-gateway; "
            "the deployed copy may differ") in text


def test_malformed_or_absent_allowlist_is_public_only(idx, fed, tmp_path):
    rep, wsp = fed
    missing = tmp_path / "no-allowlist.json"
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    lst = tmp_path / "list.json"
    lst.write_text('["board"]', encoding="utf-8")
    nonlist = tmp_path / "nonlist.json"
    nonlist.write_text('{"board": "mesh-gateway"}', encoding="utf-8")
    assert de.load_card_allowlist(str(missing)) == {}
    assert de.load_card_allowlist(str(garbage)) == {}
    assert de.load_card_allowlist(str(lst)) == {}
    assert de.load_card_allowlist(str(nonlist)) == {}
    for path in (str(missing), str(garbage), str(lst), str(nonlist)):
        ex = de.build_extract(tokens=["chat_sync"], index_path=str(idx),
                              fed_report=str(rep), structurizr_ws=str(wsp),
                              fed_docs_db="x", merged_lookup=lambda p: None,
                              project_slug="board",
                              card_allowlist_path=path)
        assert [s for s in ex["symbols"]
                if s["repo"] == "mesh-gateway"] == []
        assert "mesh-gateway" in ex["withheld"]


def test_absent_file_means_public_only_without_path(idx, fed):
    ex = _allow_build(idx, fed, ["chat_sync"], "board", None,
                      card_allowlist_path="/nonexistent-1410/allow.json")
    assert [s for s in ex["symbols"] if s["repo"] == "mesh-gateway"] == []


def test_split_visible_mirrors_public_plus_allowlist():
    reg = {"pub": {"visibility": "public"},
           "priv": {"visibility": "private"},
           "unk": {"visibility": None}}
    vis, withheld = de.split_visible(reg)
    assert vis == ["pub"] and withheld == ["priv", "unk"]
    vis, withheld = de.split_visible(reg, "board", {"board": ["priv", "ghost"]})
    assert vis == ["priv", "pub"]
    assert withheld == ["unk"]
    vis, withheld = de.split_visible(reg, "nope", {"board": ["priv"]})
    assert vis == ["pub"]  # project with no entry: public only


def test_card_project_slug_follows_board_rule():
    projs = [{"id": 6, "slug": "board"}, {"id": "13", "slug": "omega-roadmap"}]
    assert de.card_project_slug({"project_id": 6}, projs) == "board"
    assert de.card_project_slug({"project_id": "13"}, projs) == "omega-roadmap"
    assert de.card_project_slug({"project_id": 99}, projs) is None
    assert de.card_project_slug({"project_id": 6}, None) is None
    assert de.card_project_slug({"project": "board"}, projs) == "board"
    assert de.card_project_slug(None, projs) is None


# ── #1410: three separate plain lines ──

def test_missing_allowlisted_repo_named_not_in_index(idx, fed):
    ex = _allow_build(idx, fed, ["chat_sync"], "board",
                      {"board": ["mesh-gateway", "ghost-repo"]})
    assert ex["missing_repos"] == ["ghost-repo"]
    text = de.render(ex)
    assert "repo ghost-repo is not in the index" in text
    assert "no symbol resolved" not in text  # symbols DID resolve
    assert "index unavailable" not in text


def test_no_symbol_resolved_line(idx, fed):
    ex = _build(idx, fed, ["zz_no_such_symbol_anywhere"])
    assert ex["symbols"] == [] and ex["routes"] == []
    text = de.render(ex)
    assert "no symbol resolved" in text
    assert "- 0 symbols" not in text
    assert "covering tests" not in text.lower()
    assert "index unavailable" not in text
    assert "is not in the index" not in text


def test_index_unavailable_line_distinct(tmp_path, fed):
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"],
                          index_path=str(tmp_path / "no.db"),
                          fed_report=str(rep), structurizr_ws=str(wsp))
    text = de.render(ex)
    assert "index unavailable" in text
    assert "no symbol resolved" not in text
    assert "is not in the index" not in text


# ── #1410: path-component private-name filtering ──

def test_path_component_withheld_name_dropped_from_fed_lines(tmp_path):
    assert de._line_withholds(
        "  ↳ imports mesh_gateway @ src/mesh_gateway/x.py:39",
        ["mesh-gateway"], ["swarph-cli", "swarph-mesh"]) is True
    assert de._line_withholds(
        "    • swarph-cli       → (nothing depends on it)",
        ["mesh-gateway"], ["swarph-cli", "swarph-mesh"]) is False
    # ordinary filenames are not registry names: no false drop from the
    # path-component rule (a visible repo's own path stays printable).
    assert de._line_withholds(
        "  ↳ uses swarph_mesh @ src/swarph_mesh/x.py:39",
        ["mesh-gateway"],
        ["swarph-cli", "swarph-mesh", "swarph-shared"]) is False
    assert de._path_withholds("src/mesh_gateway/x.py", ["mesh-gateway"]) is True
    assert de._path_withholds("src/swarph_cli/gateway/x.py",
                              ["mesh-gateway"]) is False


def test_symbol_with_withheld_path_component_skipped(tmp_path, fed):
    p = tmp_path / "pc.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path), "public", "t", "full"))
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("mesh-gateway", "o/gw", str(tmp_path / "gw"),
                 "private", "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "chat_sync", "function",
                 "src/mesh-gateway/x.py", 3, "", "", "", ""))
    con.commit()
    con.close()
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(p),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db="x", merged_lookup=lambda p: None,
                          card_allowlist={})
    assert ex["symbols"] == []  # fail closed: path names a withheld repo
    assert "src/mesh-gateway/x.py" not in de.render(ex)


# ── #1410: underscore call/def spellings resolve ──

def _underscore_db(tmp_path):
    p = tmp_path / "u.db"
    con = sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path), "public", "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "_format_graph", "function",
                 "src/swarph_cli/x.py", 77, "_format_graph",
                 "", "def _format_graph(rows):", ""))
    con.commit()
    con.close()
    return p


def test_underscore_call_and_def_spellings_resolve(tmp_path, fed):
    rep, wsp = fed
    db = _underscore_db(tmp_path)
    for tok in ("_format_graph(rows)", "def _format_graph", "_format_graph"):
        ex = de.build_extract(tokens=[tok], index_path=str(db),
                              fed_report=str(rep), structurizr_ws=str(wsp),
                              fed_docs_db="x", merged_lookup=lambda p: None,
                              card_allowlist={})
        hit = [s for s in ex["symbols"] if s["name"] == "_format_graph"]
        assert hit and hit[0]["start_line"] == 77, tok
        assert "no symbol resolved" not in de.render(ex)
    # mining: backticked call spelling and bare def-site spelling both mine
    # the bare name; junk keeps its brackets and stays filtered.
    card = {"title": "fix `_format_graph(rows)`",
            "body": "see def _format_graph and `json.loads(row[\"menu_json`"}
    toks = de.extract_card_tokens(card)
    assert "_format_graph" in toks
    assert "json.loads" not in toks
    assert not any("(" in t or "[" in t for t in toks)


# ── #1410: board plumbing resolves the project slug ──

def test_board_dev_extract_uses_card_project_allowlist(idx, fed, monkeypatch,
                                                       capsys):
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    card = {"id": 7, "project_id": 6, "title": "t", "body": "fix chat_sync"}
    projs = {"projects": [{"id": 6, "slug": "board"}]}

    def _fake_get(url, tok, **k):
        if url.endswith("/board/cards/7"):
            return 200, card
        if url.endswith("/board/projects"):
            return 200, projs
        raise AssertionError(url)

    monkeypatch.setattr(board_mod, "_resolve_self_name",
                        lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_token",
                        lambda *_a, **_k: "tok")
    monkeypatch.setattr(board_mod, "_http_get_json", _fake_get)
    monkeypatch.setattr(de, "load_card_allowlist",
                        lambda path=None: {"board": ["mesh-gateway"]})
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7", "--print-only",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    # board project -> mesh-gateway visible: the private copy resolves.
    assert "server.py:12" in out.out
    assert "SECRET-SIG" in out.out
