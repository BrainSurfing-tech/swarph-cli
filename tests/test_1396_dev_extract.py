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


# ── rework r2 (shape-only #1410): no resolvable repo → the outcomes' own
# plain lines; the federation half prints the stamp + Layer lines ──

def test_no_visible_target_repo_prints_honest_line(idx, fed):
    ex = _build(idx, fed, ["zz_no_such_symbol_anywhere"])
    assert ex["repos_hit"] == []
    text = de.render(ex)
    # The old synthetic line is gone; the outcome's own plain lines carry it.
    assert "federation: no visible target repo" not in text
    assert "no symbol resolved" in text
    # Stamp + Layer lines still print (repo-independent, shape-only).
    assert "build: 2026-11-02T04:45:01Z" in text
    assert "Layer 2:" in text
    assert "lab-ovh" not in text  # no deploy units without a hit repo
    assert "downstream" not in text
    assert "──depends on──▶" not in text
    assert "→" not in text.split("## federation half")[1].split("WITHHELD:")[0]
    assert "imports" not in text.split("## federation half")[1].split("WITHHELD:")[0]
    assert "BLAST RADIUS" not in text


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
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
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
            or "Layer 2:" in out.out
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


# ── shape-only #1410: federation half keeps stamp + Layer lines only ──

def test_federation_half_drops_lines_naming_withheld_repo(idx, tmp_path):
    # Even when the report names a WITHHELD repo on a depends-on line, the
    # shape-only half prints neither that line nor any other non-Layer line.
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
    # Shape-only: the stamp plus the Layer header, nothing else.
    assert ex["federation_report"]["status"] == "ok"
    assert ex["federation_report"]["layers"] == ["Layer 2:"]
    # #1410: rels are never read nor printed — no downstream key at all —
    # while the deploy unit stays.
    assert "downstream" not in ex["structurizr"]
    assert "containers" not in ex["structurizr"]
    assert ex["structurizr"]["deploy_units"] == ["lab-ovh"]
    text = de.render(ex)
    fed_half = text.split("## federation half")[1].split("WITHHELD:")[0]
    assert "Layer 2:" in fed_half  # the Layer header is kept
    assert "build: 2026-11-02T04:45:01Z" in fed_half  # stamp, not mtime
    for banned in ("mesh-gateway", "mystery", "──depends on──▶", "→",
                   "imports", "downstream", "BLAST RADIUS", "nothing depends"):
        assert banned not in fed_half, banned
    assert "deploy units: lab-ovh" in fed_half
    assert "downstream" not in text.lower()


# ── #3757(c): --json redacts WITHHELD registry entries ──

def test_json_redacts_withheld_registry_paths(idx, fed, monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    monkeypatch.setattr(board_mod, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
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
    # Shape-only #1410: no per-repo line is kept at all — dotted private
    # paths, slashed private paths, and unregistered names all vanish with
    # the depends-on/arrow/import lines that carried them.
    p = _r5_db(tmp_path)
    rep, wsp = _r5_sources(tmp_path)
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(p),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(tmp_path / "no-fed.db"),
                          merged_lookup=lambda p: None)
    assert ex["federation_report"]["layers"] == ["Layer 2:"]
    text = de.render(ex)
    fed_half = text.split("## federation half")[1].split("WITHHELD:")[0]
    assert "Layer 2:" in fed_half
    for banned in ("proven", "mesh_gateway", "swarph-panel", "swarph-mesh",
                   "secret_mod", "private_helper", "──depends on──▶", "→",
                   "imports", "nothing depends"):
        assert banned not in fed_half, banned


def test_structurizr_drops_withheld_and_unregistered_rels(tmp_path):
    # #1410: rels are never read nor printed — no downstream key, no rel
    # text — while deploy units stay (shape-only fed half needs no filtering).
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
    # Entitled composition (the pre-entitlement allowlist-always-applies
    # behavior); unentitled composition is pinned separately below.
    kw.setdefault("entitled", True)
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
    # Default is public only even when an allowlist is passed in: the
    # allowlist contributes private repos only with entitled=True (#1410).
    vis, withheld = de.split_visible(reg, "board", {"board": ["priv", "ghost"]})
    assert vis == ["pub"] and withheld == ["priv", "unk"]
    vis, withheld = de.split_visible(
        reg, "board", {"board": ["priv", "ghost"]}, entitled=True)
    assert vis == ["priv", "pub"]
    assert withheld == ["unk"]
    vis, withheld = de.split_visible(reg, "nope", {"board": ["priv"]},
                                     entitled=True)
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

def test_path_component_withheld_name_dropped_from_fed_lines(idx, fed):
    # Shape-only #1410: the fed half keeps Layer headers regardless of which
    # repos (withheld or not) the report lines name — even an imports line
    # naming a withheld repo leaves no trace, because the line is not kept.
    rep, wsp = fed
    ex = _build(idx, fed, ["chat_sync"])
    fed_half = de.render(ex).split("## federation half")[1].split("WITHHELD:")[0]
    assert "mesh-gateway" not in fed_half
    assert "mystery" not in fed_half
    # ordinary filenames are not registry names: no false drop from the
    # path-component rule (a visible repo's own path stays printable).
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
        if url.endswith("/peers/lab-ovh/grants"):
            return 200, {"peer": "lab-ovh", "groups": [],
                         "grants": [{"grant_type": "board", "target": "board",
                                     "level": "execute", "via_group": None,
                                     "direct": True}]}
        raise AssertionError(url)

    monkeypatch.setattr(board_mod, "_resolve_self_name",
                        lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
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
    # board project + direct execute grant -> mesh-gateway visible.
    assert "server.py:12" in out.out
    assert "SECRET-SIG" in out.out
    assert ("private rows: shown for project board "
            "(runner lab-ovh via --as: direct execute grant; "
            "repos from card-allowlist)") in out.out


# ── shape-only #1410: a card-named repo with no index row gets its own line ──

def test_card_named_missing_repo_prints_not_in_index(idx, fed):
    # Card #1393 shape: the body names `swarph-codegraph-index`, which has
    # no repos row here — the extract says so plainly instead of silence.
    card = {"id": 1393,
            "title": "codegraph index is silently stale across the fleet",
            "body": "swarph-codegraph-index codegraph_index/fleet.py:89-91 "
                    "sets dm_target for these private repos"}
    rep, wsp = fed
    ex = de.build_extract(tokens=["chat_sync"], index_path=str(idx),
                          fed_report=str(rep), structurizr_ws=str(wsp),
                          fed_docs_db=str(rep) + ".nodb",
                          merged_lookup=lambda p: None, card=card)
    assert ex["missing_repos"] == ["swarph-codegraph-index"]
    text = de.render(ex)
    assert "repo swarph-codegraph-index is not in the index" in text
    assert "index unavailable" not in text


def test_card_named_missing_repo_ignores_non_repo_words(idx, fed):
    # Only fleet-shaped `swarph-...` names count: hyphenated prose
    # (`supply-chain`), peer names (`lab-ovh`), and modules
    # (`codegraph_index`) never print a repo line — nothing is invented.
    card = {"id": 1, "title": "supply-chain cleanup",
            "body": "ask lab-ovh about codegraph_index and first-time setup; "
                    "fix chat_sync"}
    ex = _build(idx, fed, ["chat_sync"], card=card)
    assert ex["missing_repos"] == []
    assert "is not in the index" not in de.render(ex)


# ── shape-only #1410: LIKE metacharacters in --path match literally ──

def test_path_like_metacharacters_match_literally(idx, fed):
    import sqlite3 as _sqlite3
    rep, wsp = fed
    con = _sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    try:
        reg = de.repo_registry(con)
        vis, _ = de.split_visible(reg)
        assert de.find_symbols_by_path(con, "%", vis) == []
        assert de.find_symbols_by_path(con, "_", vis) == []
        assert de.find_symbols_by_path(con, "\\", vis) == []
        assert de.find_symbols_by_path(
            con, "src/swarph_cli/gateway/server.py", vis) != []
    finally:
        con.close()


# ── #1410: readable card with no tokens exits 0; unreadable keeps exit 1 ──

def test_board_readable_card_without_tokens_exits_zero(idx, fed, monkeypatch,
                                                       capsys):
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    card = {"id": 99, "title": "rework cap",
            "body": "run `pip install -U` and `import json, sys` only"}
    monkeypatch.setattr(board_mod, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
    monkeypatch.setattr(board_mod, "_resolve_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr(board_mod, "_http_get_json",
                        lambda url, tok, **k: (200, card))
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "99", "--print-only",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    assert "card #99: no symbols to look up" in out.out
    assert "no symbol resolved" in out.out
    assert "Traceback" not in out.out and "Traceback" not in out.err


def test_board_unreadable_card_without_tokens_keeps_exit_1(idx, fed,
                                                           monkeypatch,
                                                           capsys):
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    monkeypatch.setattr(board_mod, "_resolve_self_name", lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
    monkeypatch.setattr(board_mod, "_resolve_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr(board_mod, "_http_get_json",
                        lambda url, tok, **k: (404, {"detail": "no such card"}))
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "99", "--print-only",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 1
    assert "cannot read card #99" in out.err


# ── #1410 rework (#3903): entitlement reads the runner's OWN direct row ──

def _direct_row(level="execute", target="board"):
    """An exact-grantee board grant row, as GET /peers/{peer}/grants reports it."""
    return {"grant_type": "board", "target": target, "level": level,
            "via_group": None, "direct": True}


def _group_row(level, group, target="board"):
    """A group-inherited board grant row (direct is never the runner's own)."""
    return {"grant_type": "board", "target": target, "level": level,
            "via_group": group, "direct": False}


def _wide_row(level, target="board"):
    """A '*' grantee board grant row: via_group None, direct False."""
    return {"grant_type": "board", "target": target, "level": level,
            "via_group": None, "direct": False}


def _fake_board_env(monkeypatch, *, grants, grants_status=200,
                    grants_exc=None, card=None, projects=None,
                    card_status=200, thread_uuid=None):
    """Fake the gateway seam for a dev-extract run; return posted payloads.

    `grants` is the `grants` list of GET /peers/lab-ovh/grants (or an
    exception via `grants_exc`, a status via `grants_status`, a card fetch
    status via `card_status`). POST /messages is captured, never sent.
    """
    from swarph_cli.commands import board as board_mod
    _card = {"id": 7, "project_id": 6, "title": "t",
             "body": "fix chat_sync"} if card is None else card
    if thread_uuid is not None:
        _card = dict(_card, thread_uuid=thread_uuid, assignee="lab-ovh")
    _projs = {"projects": [{"id": 6, "slug": "board"}]} \
        if projects is None else projects
    posted = {}

    def _fake_get(url, tok, **k):
        if url.endswith("/board/cards/7"):
            if card_status != 200:
                return card_status, {"detail": "down"}
            return 200, _card
        if url.endswith("/board/projects"):
            return 200, _projs
        if url.endswith("/peers/lab-ovh/grants"):
            if grants_exc is not None:
                raise grants_exc
            return grants_status, {"peer": "lab-ovh", "groups": [],
                                   "grants": grants}
        raise AssertionError(url)

    def _fake_post(url, body, tok, **k):
        if url.endswith("/messages"):
            posted["body"] = body
            return 200, {"id": 4242}
        raise AssertionError(url)

    monkeypatch.setattr(board_mod, "_resolve_self_name",
                        lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
    monkeypatch.setattr(board_mod, "_resolve_token",
                        lambda *_a, **_k: "tok")
    monkeypatch.setattr(board_mod, "_http_get_json", _fake_get)
    monkeypatch.setattr(board_mod, "_post_json", _fake_post)
    monkeypatch.setattr(de, "load_card_allowlist",
                        lambda path=None: {"board": ["mesh-gateway"]})
    return posted


def _print_only_argv(idx, fed):
    rep, wsp = fed
    return (["cards", "dev-extract", "--card", "7", "--print-only",
             "--index", str(idx), "--fed-report", str(rep),
             "--structurizr-ws", str(wsp)])


SHOWN_LINE = ("private rows: shown for project board "
                "(runner lab-ovh via --as: direct execute grant; "
                "repos from card-allowlist)")


def test_execute_grant_sees_private_row(idx, fed, monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    posted = _fake_board_env(monkeypatch, grants=[_direct_row()])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" in out.out  # the private file:line prints
    assert "SECRET-SIG" in out.out
    assert SHOWN_LINE in out.out
    assert posted == {}  # print-only posts nothing


def test_propose_via_all_grant_shows_public_only(idx, fed, monkeypatch,
                                                 capsys):
    from swarph_cli.commands import board as board_mod
    _fake_board_env(monkeypatch, grants=[_group_row("propose", "ALL")])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" not in out.out
    assert "SECRET-SIG" not in out.out
    assert ("private rows: withheld for project board "
            "(runner lab-ovh via --as: no direct execute grant; "
            "best board grant: level=propose, via=ALL)") in out.out


def test_direct_propose_grant_shows_public_only(idx, fed, monkeypatch,
                                               capsys):
    from swarph_cli.commands import board as board_mod
    _fake_board_env(monkeypatch, grants=[_direct_row("propose")])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" not in out.out
    assert "SECRET-SIG" not in out.out
    assert ("private rows: withheld for project board "
            "(runner lab-ovh via --as: no direct execute grant; "
            "best board grant: level=propose, via=direct)") in out.out


def test_no_board_grant_names_no_grant(idx, fed, monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    _fake_board_env(monkeypatch, grants=[])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" not in out.out
    assert ("private rows: withheld for project board "
            "(runner lab-ovh via --as: no direct execute grant; "
            "no board grant at all)") in out.out


def test_grant_endpoint_error_is_public_only(idx, fed, monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    for grants, status in ([], 500), ("not-a-dict", 200), (None, 200):
        _fake_board_env(monkeypatch, grants=grants, grants_status=status)
        rc = board_mod.run_board(_print_only_argv(idx, fed))
        out = capsys.readouterr()
        assert rc == 0, (grants, status)
        assert "server.py:12" not in out.out, (grants, status)
        assert "SECRET-SIG" not in out.out, (grants, status)
        assert ("private rows: withheld for project board "
                "(runner lab-ovh via --as: grant check unavailable)") in out.out


def test_grant_endpoint_unreachable_says_unreachable(idx, fed, monkeypatch,
                                                    capsys):
    from swarph_cli.commands import board as board_mod
    for grants_exc, status in ((RuntimeError("boom"), 200), (None, 0)):
        _fake_board_env(monkeypatch, grants=[], grants_status=status,
                        grants_exc=grants_exc)
        rc = board_mod.run_board(_print_only_argv(idx, fed))
        out = capsys.readouterr()
        assert rc == 0, (grants_exc, status)
        assert "server.py:12" not in out.out, (grants_exc, status)
        assert ("private rows: withheld for project board "
                "(runner lab-ovh via --as: gateway unreachable, "
                "so no grant to check)") in out.out, (grants_exc, status)


def test_post_path_with_execute_grant_posts_public_only(idx, fed, monkeypatch,
                                                        capsys):
    from swarph_cli.commands import board as board_mod
    posted = _fake_board_env(monkeypatch, thread_uuid="thr-1",
                             grants=[_direct_row()])
    rep, wsp = fed
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    content = posted["body"]["content"]
    assert "server.py:12" not in content  # no private row in the post
    assert "SECRET-SIG" not in content
    assert "mesh-gateway" not in content  # no withheld NAMES (no oracle)
    assert "mystery" not in content
    assert "also defined in withheld" not in content
    assert ("WITHHELD: 2 repos "
            "(names not shown in posted extracts)") in content
    # The terminal still states the runner's own entitlement basis.
    assert SHOWN_LINE in out.out


def test_print_path_same_execute_runner_shows_row(idx, fed, monkeypatch,
                                                  capsys):
    from swarph_cli.commands import board as board_mod
    _fake_board_env(monkeypatch, grants=[_direct_row()])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" in out.out
    assert "WITHHELD: mystery" in out.out  # print mode keeps names


def test_post_safe_json_stdout_carries_no_withheld_names(idx, fed,
                                                         monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    _fake_board_env(monkeypatch, thread_uuid="thr-1",
                    grants=[_direct_row()])
    rep, wsp = fed
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7", "--json",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    dumped = json.dumps(payload)
    assert "server.py:12" not in dumped
    assert "mesh-gateway" not in dumped
    assert "mystery" not in dumped
    assert payload["withheld"] == []
    assert payload["withheld_count"] == 2
    assert SHOWN_LINE in out.err


def test_no_card_prints_no_card_line(idx, fed, monkeypatch, capsys):
    from swarph_cli.commands import board as board_mod
    monkeypatch.setattr(board_mod, "_resolve_self_name",
                        lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
    monkeypatch.setattr(board_mod, "_resolve_token",
                        lambda *_a, **_k: "tok")
    rep, wsp = fed
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--symbol", "chat_sync", "--print-only",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    assert ("private rows: withheld "
            "(no card, so no project grant to check)") in out.out
    assert "server.py:12" not in out.out  # public rows only
    assert "SECRET-SIG" not in out.out


def test_unreadable_card_with_explicit_symbol_is_no_card(idx, fed, monkeypatch,
                                                         capsys):
    from swarph_cli.commands import board as board_mod
    monkeypatch.setattr(board_mod, "_resolve_self_name",
                        lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
    monkeypatch.setattr(board_mod, "_resolve_token",
                        lambda *_a, **_k: "tok")
    monkeypatch.setattr(board_mod, "_http_get_json",
                        lambda url, tok, **k: (404, {"detail": "no card"}))
    rep, wsp = fed
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7", "--symbol", "chat_sync",
         "--print-only", "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    assert ("private rows: withheld "
            "(no card, so no project grant to check)") in out.out
    assert "server.py:12" not in out.out


def test_unentitled_build_is_public_only_by_default(idx, fed):
    # Default (no entitled flag) ignores the allowlist entirely — even the
    # allowlist-derived missing-repo names stay out (no existence oracle).
    ex = _allow_build(idx, fed, ["chat_sync"], "board",
                      {"board": ["mesh-gateway", "ghost-repo"]},
                      entitled=False)
    assert [s for s in ex["symbols"] if s["repo"] == "mesh-gateway"] == []
    assert "mesh-gateway" in ex["withheld"]
    assert ex["missing_repos"] == []
    text = de.render(ex)
    assert "server.py:12" not in text
    assert "ghost-repo" not in text


def test_render_post_safe_hides_all_withheld_names(idx, fed):
    ex = _allow_build(idx, fed, ["chat_sync"], "board",
                      {"board": ["mesh-gateway"]}, entitled=False)
    printed = de.render(ex)
    assert "mesh-gateway" in printed  # print mode keeps names
    safe = de.render(ex, post_safe=True)
    assert "mesh-gateway" not in safe
    assert "mystery" not in safe
    assert "also defined in withheld" not in safe
    assert ("WITHHELD: 2 repos "
            "(names not shown in posted extracts)") in safe
    assert "index coverage is partial: a withheld repo's main module" in safe


def test_redact_post_safe_strips_withheld_names(idx, fed):
    import json as _json
    ex = _allow_build(idx, fed, ["chat_sync"], "board",
                      {"board": ["mesh-gateway"]}, entitled=False)
    plain = de.redact_for_json(ex)
    assert "mesh-gateway" in _json.dumps(plain)  # print-shape keeps names
    safe = de.redact_for_json(ex, post_safe=True)
    dumped = _json.dumps(safe)
    assert "mesh-gateway" not in dumped
    assert "mystery" not in dumped
    assert safe["withheld"] == []
    assert safe["withheld_count"] == 2
    assert safe["withheld_hits"] == {}
    assert "swarph-cli" in safe["registry"]  # visible repos keep everything


def test_privacy_line_texts():
    from swarph_cli.commands.board import _dev_extract_privacy_line as line
    kw = {"runner": "lab-ovh", "source": "--as"}
    assert line(None, None, False, None, None, "ok", **kw) == \
        "private rows: withheld (no card, so no project grant to check)"
    assert line(None, None, False, None, None, "ok", **kw,
                gw_unreachable=True) == \
        ("private rows: withheld "
         "(gateway unreachable, so no project grant to check)")
    assert line({"id": 1}, None, False, None, None, "ok", **kw) == \
        ("private rows: withheld (project unresolvable, "
         "so no project grant to check)")
    assert line({"id": 1}, "board", True, "execute", None, "ok", **kw) == \
        ("private rows: shown for project board "
         "(runner lab-ovh via --as: direct execute grant; "
         "repos from card-allowlist)")
    # F2: a best level=execute that is not the runner's own direct grant
    # never prints under a "no execute grant" claim.
    assert line({"id": 1}, "board", False, "execute", "ALL", "ok", **kw) == \
        ("private rows: withheld for project board "
         "(runner lab-ovh via --as: no direct execute grant; "
         "best board grant: level=execute, via=ALL)")
    assert line({"id": 1}, "board", False, "propose", "ALL", "ok", **kw) == \
        ("private rows: withheld for project board "
         "(runner lab-ovh via --as: no direct execute grant; "
         "best board grant: level=propose, via=ALL)")
    assert line({"id": 1}, "board", False, None, None, "ok", **kw) == \
        ("private rows: withheld for project board "
         "(runner lab-ovh via --as: no direct execute grant; "
         "no board grant at all)")
    assert line({"id": 1}, "board", False, None, None, "unavailable",
                **kw) == \
        ("private rows: withheld for project board "
         "(runner lab-ovh via --as: grant check unavailable)")
    assert line({"id": 1}, "board", False, None, None, "unreachable",
                **kw) == \
        ("private rows: withheld for project board "
         "(runner lab-ovh via --as: gateway unreachable, "
         "so no grant to check)")


# ── #1410 rework (#3903 F1/F2): the direct-row rule vs live gateway logic ──

def _live_gateway_harness(tmp_path):
    """Live board-grant union over a temp db (msg 88033 §F1/F2).

    Harness method: the GROUP half lifts the REAL shipped gateway functions
    (``server._peer_groups`` / ``server._group_grants_for`` /
    ``server._GRANT_LEVEL_RANK``) over tables built from the REAL
    ``gateway/schema.sql`` — this fork states that surface is behaviourally
    identical to the deployed gateway. The BOARD-direct half does not exist
    in this fork (it denies grant_type=board by design), so it models the
    live semantics exactly as #3903 documents them: the verdict is the MAX
    over the exact-grantee direct row, the '*' row (when it applies), and
    the group rows, with ``via_group`` named only for a matching group row.
    Returns ``(live_verdict, peer_grants_payload)`` builders.
    """
    import pathlib as _pathlib
    import sqlite3 as _sqlite3

    # Same gate as the repo's other gateway-lifting tests: the harness
    # imports the real server module, which needs fastapi (absent on the
    # windows CI image — those 4 tests skip there, run everywhere else).
    pytest.importorskip("fastapi")
    from swarph_cli.gateway import server
    schema_path = _pathlib.Path(server.__file__).with_name("schema.sql")
    db_path = str(tmp_path / "live.db")
    con = _sqlite3.connect(db_path)
    con.row_factory = _sqlite3.Row
    con.executescript(schema_path.read_text(encoding="utf-8"))
    return con, server


def _seed_live(con, peer, *, ratified=True, groups=(), group_grants=()):
    """Seed peers/groups/memberships/grants; group_grants bypasses the local
    400 guard by direct SQL (the deployed gateway accepts board group
    grants — F2's group:ALL execute exists there)."""
    con.execute(
        "INSERT INTO claude_peers (name, url, capabilities, registered_at,"
        " last_health, last_seen, enabled, ratified) VALUES (?,?,?,?,?,?,?,?)",
        (peer, "http://x/", "{}", "2026-01-01T00:00:00+00:00", None, None,
         1, 1 if ratified else 0))
    for g in dict.fromkeys(["ALL", *groups]):
        if g == "ALL":
            continue
        con.execute(
            "INSERT OR IGNORE INTO groups (name, kind, created_by, created_at)"
            " VALUES (?,?,?,?)", (g, "custom", "t", "t"))
    for g in groups:
        con.execute(
            'INSERT OR IGNORE INTO group_members ("group", peer, added_by,'
            " added_at) VALUES (?,?,?,?)", (g, peer, "t", "t"))
    for g, gtype, target, level in group_grants:
        con.execute(
            'INSERT OR IGNORE INTO group_grants ("group", grant_type, target,'
            " level, added_by, added_at) VALUES (?,?,?,?,?,?)",
            (g, gtype, target, level, "t", "t"))
    con.commit()


def _live_verdict(con, server, peer, slug, direct_rows, star_rows):
    """The live gateway's effective verdict for (peer, board, slug).

    Direct rows: ``[(grantee, target, level)]`` with exact-grantee and '*'
    entries; group half: the REAL ``server._group_grants_for`` rows. Best
    by the REAL ``server._GRANT_LEVEL_RANK``; on a direct/group tie at the
    same level the group row wins the ``via_group`` (the observed F2), and
    a '*' match reports ``via_group None`` (the observed F1).
    """
    cands = []  # (level, via_group); group rows sort before direct on ties
    for r in server._group_grants_for(con, peer, "board", slug):
        cands.append((r["level"], r["grp"], 0))
    for grantee, target, level in star_rows:
        if target == slug:
            cands.append((level, None, 1))
    for grantee, target, level in direct_rows:
        if target == slug and grantee == peer:
            cands.append((level, None, 2))
    if not cands:
        return {"allow": False, "via_group": None, "level": None}
    # Best rank wins; on a rank tie the group row wins the via_group (the
    # observed live F2 order), then '*' rows, then the exact direct row.
    top = max(server._GRANT_LEVEL_RANK.get(t[0], 0) for t in cands)
    best = min((t for t in cands
                if server._GRANT_LEVEL_RANK.get(t[0], 0) == top),
               key=lambda t: t[2])
    return {"allow": True, "via_group": best[1], "level": best[0]}


def _peer_payload(con, server, peer, slug, direct_rows, star_rows):
    """The deployed GET /peers/{peer}/grants board rows for this state."""
    rows = []
    for r in server._group_grants_for(con, peer, "board", slug):
        rows.append({"grant_type": "board", "target": slug,
                     "level": r["level"], "via_group": r["grp"],
                     "direct": False})
    for grantee, target, level in star_rows:
        if target == slug:
            rows.append({"grant_type": "board", "target": target,
                         "level": level, "via_group": None, "direct": False})
    for grantee, target, level in direct_rows:
        if target == slug and grantee == peer:
            rows.append({"grant_type": "board", "target": target,
                         "level": level, "via_group": None, "direct": True})
    return {"peer": peer, "groups": server._peer_groups(con, peer),
            "grants": rows}


def _old_via_group_rule(live):
    """The removed check: allow + execute + via_group None reads as direct."""
    return (live.get("allow") is True and live.get("level") == "execute"
            and live.get("via_group") is None)


def test_live_star_execute_reports_via_none(tmp_path):
    """F1 live shape: '*' at execute resolves allow + via_group None."""
    con, server = _live_gateway_harness(tmp_path)
    _seed_live(con, "lab-ovh", groups=["ALL"],
               group_grants=[("ALL", "board", "board", "propose")])
    star = [("*", "board", "execute")]
    live = _live_verdict(con, server, "lab-ovh", "board", [], star)
    assert live == {"allow": True, "via_group": None, "level": "execute"}
    # A name with no peer row at all reads the same from the '*' row.
    live_unknown = _live_verdict(con, server, "ghost-peer", "board", [], star)
    assert live_unknown == {"allow": True, "via_group": None,
                            "level": "execute"}


def test_live_direct_plus_all_reports_via_all(tmp_path):
    """F2 live shape: direct execute + group ALL execute reads via=ALL."""
    con, server = _live_gateway_harness(tmp_path)
    _seed_live(con, "lab-ovh", groups=["ALL"],
               group_grants=[("ALL", "board", "board", "execute")])
    live = _live_verdict(con, server, "lab-ovh", "board",
                         [("lab-ovh", "board", "execute")], [])
    assert live == {"allow": True, "via_group": "ALL", "level": "execute"}


def test_star_execute_is_not_entitlement(idx, fed, monkeypatch, capsys,
                                         tmp_path):
    """F1: a '*' execute grant never entitles, whoever the runner is."""
    from swarph_cli.commands import board as board_mod
    con, server = _live_gateway_harness(tmp_path)
    _seed_live(con, "lab-ovh", groups=["ALL"],
               group_grants=[("ALL", "board", "board", "propose")])
    star = [("*", "board", "execute")]
    live = _live_verdict(con, server, "lab-ovh", "board", [], star)
    assert _old_via_group_rule(live) is True  # the removed check opens here
    payload = _peer_payload(con, server, "lab-ovh", "board", [], star)
    assert [g for g in payload["grants"] if g.get("direct") is True] == []
    _fake_board_env(monkeypatch, grants=payload["grants"])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" not in out.out  # still public only
    assert "SECRET-SIG" not in out.out
    assert "shown for project board" not in out.out
    assert ("best board grant: level=execute, via=wide") in out.out


def test_direct_execute_with_group_all_stays_entitled(idx, fed, monkeypatch,
                                                      capsys, tmp_path):
    """F2: direct execute entitles whatever else matches; no false line."""
    from swarph_cli.commands import board as board_mod
    con, server = _live_gateway_harness(tmp_path)
    _seed_live(con, "lab-ovh", groups=["ALL"],
               group_grants=[("ALL", "board", "board", "execute")])
    direct = [("lab-ovh", "board", "execute")]
    live = _live_verdict(con, server, "lab-ovh", "board", direct, [])
    assert _old_via_group_rule(live) is False  # the removed check closes here
    payload = _peer_payload(con, server, "lab-ovh", "board", direct, [])
    _fake_board_env(monkeypatch, grants=payload["grants"])
    rc = board_mod.run_board(_print_only_argv(idx, fed))
    out = capsys.readouterr()
    assert rc == 0
    assert "server.py:12" in out.out  # entitled: the private row prints
    assert SHOWN_LINE in out.out
    assert "no execute grant" not in out.out
    assert "no direct execute grant" not in out.out


def test_no_false_no_execute_grant_line_anywhere(idx, fed, monkeypatch,
                                                 capsys):
    """Acceptance 2, second clause: the false line is gone in every shape."""
    from swarph_cli.commands import board as board_mod
    shapes = [
        [_direct_row()],
        [_direct_row(), _group_row("execute", "ALL")],
        [_direct_row(), _wide_row("execute")],
        [_wide_row("execute")],
        [_group_row("execute", "ALL")],
        [_group_row("execute", "peer:x")],
        [_direct_row("propose")],
        [],
    ]
    for grants in shapes:
        _fake_board_env(monkeypatch, grants=grants)
        rc = board_mod.run_board(_print_only_argv(idx, fed))
        out = capsys.readouterr()
        assert rc == 0, grants
        assert "no execute grant" not in out.out, grants


def test_entitlement_reads_direct_row_not_best_level(monkeypatch):
    """The predicate keys on exact-grantee direct rows, never the best row."""
    from swarph_cli.commands import board as board_mod

    def _run(grants):
        monkeypatch.setattr(board_mod, "_http_get_json",
                            lambda url, tok, **k: (200, {"grants": grants}))
        return board_mod._dev_extract_entitlement(
            "http://gw", "tok", "lab-ovh", "board")

    assert _run([_direct_row()])[0] is True
    assert _run([_direct_row(), _group_row("execute", "ALL")])[0] is True
    assert _run([_group_row("execute", "ALL")])[0] is False
    assert _run([_wide_row("execute")])[0] is False
    assert _run([_direct_row("propose")])[0] is False
    # Other targets and other grant types never count.
    assert _run([_direct_row("execute", target="other")])[0] is False
    assert _run([{"grant_type": "channel", "target": "board",
                  "level": "execute", "via_group": None,
                  "direct": True}])[0] is False


def test_gateway_unreachable_card_says_unreachable(idx, fed, monkeypatch,
                                                   capsys):
    """Acceptance 3: an unreachable gateway never reads as 'no card'."""
    from swarph_cli.commands import board as board_mod
    # Status-0 card fetch with explicit symbols: public-only, rc 0.
    _fake_board_env(monkeypatch, grants=[], card_status=0)
    rep, wsp = fed
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7", "--symbol", "chat_sync",
         "--print-only", "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    assert ("private rows: withheld "
            "(gateway unreachable, so no project grant to check)") in out.out
    assert "no card, so no project grant to check" not in out.out
    # A raising card fetch reads the same, and a tokenless run exits 1
    # naming the gateway, not the card.
    monkeypatch.setattr(board_mod, "_http_get_json",
                        lambda url, tok, **k: (_ for _ in ()).throw(
                            ConnectionError("down")) if "cards/7" in url
                        else (200, {"projects": [{"id": 6, "slug": "board"}]}))
    monkeypatch.setattr(board_mod, "_resolve_self_name",
                        lambda *_a, **_k: "lab-ovh")
    monkeypatch.setattr(board_mod, "_resolve_self_with_source",
                        lambda *_a, **_k: ("lab-ovh", "--as"))
    monkeypatch.setattr(board_mod, "_resolve_token",
                        lambda *_a, **_k: "tok")
    monkeypatch.setattr(de, "load_card_allowlist", lambda path=None: {})
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7", "--symbol", "chat_sync",
         "--print-only", "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 0
    assert "gateway unreachable" in out.out
    rc = board_mod.run_board(
        ["cards", "dev-extract", "--card", "7", "--print-only",
         "--index", str(idx), "--fed-report", str(rep),
         "--structurizr-ws", str(wsp)])
    out = capsys.readouterr()
    assert rc == 1
    assert "gateway unreachable" in out.err


def test_unentitled_posts_hide_missing_repos(fed, tmp_path):
    """Acceptance 4: ghost-named vs private-named posts read the same."""
    import sqlite3 as _sqlite3
    p = tmp_path / "priv.db"
    con = _sqlite3.connect(str(p))
    con.execute(REPOS_DDL)
    con.execute(SYMBOLS_DDL)
    con.execute(EDGES_DDL)
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-cli", "s", str(tmp_path), "public", "t", "full"))
    con.execute("INSERT INTO repos VALUES (?,?,?,?,?,?)",
                ("swarph-zz-priv", "o/p", str(tmp_path), "private",
                 "t", "full"))
    con.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?)",
                (1, "swarph-cli", "chat_sync", "function", "x.py", 3,
                 "", "", "", ""))
    con.commit()
    con.close()
    ghost = {"id": 7, "title": "t", "body": "fix chat_sync in swarph-zz-ghost"}
    real = {"id": 7, "title": "t", "body": "fix chat_sync in swarph-zz-priv"}
    rep, wsp = fed
    kw = {"index_path": str(p), "fed_report": str(rep),
          "structurizr_ws": str(wsp), "fed_docs_db": str(rep) + ".nodb",
          "merged_lookup": lambda p: None, "project_slug": "board",
          "card_allowlist": {"board": ["swarph-zz-priv"]}, "entitled": False}
    ex_ghost = de.build_extract(tokens=["chat_sync"], card=ghost, **kw)
    ex_real = de.build_extract(tokens=["chat_sync"], card=real, **kw)
    assert ex_ghost["missing_repos"] == ["swarph-zz-ghost"]
    assert ex_real["missing_repos"] == []
    assert (de.render(ex_ghost, post_safe=True)
            == de.render(ex_real, post_safe=True))
    import json as _json
    assert (_json.dumps(de.redact_for_json(ex_ghost, post_safe=True),
                        sort_keys=True, default=str)
            == _json.dumps(de.redact_for_json(ex_real, post_safe=True),
                           sort_keys=True, default=str))


def test_unentitled_post_bodies_match_live(idx, fed, monkeypatch, capsys):
    """Acceptance 4 end to end: the two posted bodies are identical."""
    from swarph_cli.commands import board as board_mod
    rep, wsp = fed
    bodies = []
    for body in ("fix chat_sync in swarph-ghost-zzz",
                 "fix chat_sync in swarph-mesh-gateway"):
        posted = _fake_board_env(
            monkeypatch, thread_uuid="thr-1", grants=[],
            card={"id": 7, "project_id": 6, "title": "t", "body": body})
        rc = board_mod.run_board(
            ["cards", "dev-extract", "--card", "7",
             "--index", str(idx), "--fed-report", str(rep),
             "--structurizr-ws", str(wsp)])
        out = capsys.readouterr()
        assert rc == 0
        assert "is not in the index" not in posted["body"]["content"]
        bodies.append(posted["body"]["content"])
    assert bodies[0] == bodies[1]
