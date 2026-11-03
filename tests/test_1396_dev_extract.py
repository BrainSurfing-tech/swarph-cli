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


def test_zero_routes_tests_downstream_print_floors(idx, fed):
    ex = _build(idx, fed, ["board_cards"])
    text = de.render(ex)
    assert "0 routes (floor: route registry not linked for this symbol)" in text
    assert ("0 covering tests (floor: name-match only, semantic coverage "
            "not linked)") in text


def test_missing_index_prints_unavailable_never_zero(tmp_path, fed):
    rep, wsp = fed
    ex = de.build_extract(tokens=["x"], index_path=str(tmp_path / "no.db"),
                          fed_report=str(rep), structurizr_ws=str(wsp))
    assert "unavailable" in ex["index"]["status"]
    assert " 0 " not in de.render(ex).split("targets:")[0]


def test_structurizr_container_match_names_deploy_unit(idx, fed):
    ex = _build(idx, fed, ["chat_sync"])
    assert ex["structurizr"]["status"] == "ok"
    assert ex["structurizr"]["deploy_units"] == ["lab-ovh"]
    assert ex["structurizr"]["downstream"] == ["swarph-mesh (imports)"]


def test_file_stem_surfaces_test_suite(idx, fed):
    # chat_sync lives in gateway/server.py: stem 'server' must surface the
    # tests/test_server.py FILE row (name-match alone only finds the test
    # whose name contains the symbol).
    ex = _build(idx, fed, ["chat_sync"])
    names = {(t["name"], t["file_path"]) for t in ex["tests"]}
    assert ("test_server.py", "tests/test_server.py") in names
    assert ("test_chat_sync_requires_auth", "tests/test_server.py") in names
    # ... but must not leak into an unrelated symbol's extract.
    ex2 = _build(idx, fed, ["board_cards"])
    names2 = {(t["name"], t["file_path"]) for t in ex2["tests"]}
    assert ("test_server.py", "tests/test_server.py") not in names2


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
    assert ex["withheld_hits"].get("chat_sync") == ["mesh-gateway", "mystery"]
    text = de.render(ex)
    # Visible copy printed in full ...
    assert "src/swarph_cli/gateway/server.py:3950" in text
    # ... withheld twin NAMED with the honest line ...
    assert ("- chat_sync: also defined in withheld mesh-gateway; "
            "the deployed copy may differ") in text
    assert ("- chat_sync: also defined in withheld mystery; "
            "the deployed copy may differ") in text
    # ... and no private detail anywhere: every mesh-gateway/mystery line is
    # either the WITHHELD notice or the also-defined line.
    for line in text.splitlines():
        if "mesh-gateway" in line or "mystery" in line:
            assert ("WITHHELD" in line or "also defined in withheld" in line), line
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
            or "0 symbols" in out.out)


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
