"""Card #1405: ``swarph skunkworks`` CLI. Fakes only: no queue, no claude, no gateway."""
import json
import subprocess
from datetime import datetime, timezone

import pytest

from swarph_cli.commands import skunkworks as cli

service = pytest.importorskip(
    "swarph_mesh.skunkworks.service", reason="needs swarph-mesh>=0.9.1 (card #1405)"
)
default_runners = service.default_runners

SAT = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
IDEA = {"concept_name": "Stub", "proposal_kind": "tooling", "mechanism": "m",
        "key_assumptions": [], "required_research_queries": ["qa", "qb"]}


class Gw:
    def __init__(self, idea=None, arch_verdict="APPROVED_FOR_BUILD"):
        self.bodies = []
        self.idea = idea or IDEA
        self.arch = {"verdict": arch_verdict, "mechanism": "m", "audit": "a"}

    def post(self, url, body):
        self.bodies.append(body)
        return 200, {}

    def get(self, url):
        body = self.bodies[-1]
        stage = body["jobs"][0]["purpose"].split("-")[1]
        lane, model, out = (("agy", "gemini-4-argon-high", self.idea) if stage == "ideation"
                            else ("claude", "claude-opus-5-5", self.arch))
        res = {"text": json.dumps(out), "served_lane": lane, "served_model": model}
        return 200, {"jobs": [{"status": "done", "result_json": json.dumps(res)}]}


def claude(argv, **kw):
    return subprocess.CompletedProcess(
        argv, 0, json.dumps({"findings": "f", "urls": ["https://example.org"]}), "")


class Live:
    status_code = 200


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "cell-a")
    tok = tmp_path / "tok"
    tok.write_text("test-token-value\n")
    seed = tmp_path / "seed.txt"
    seed.write_text("how should cells share context\n")
    posts = []

    def post_card(card, kind, content):
        posts.append((card, kind, content))

    def factory(gw):
        return default_runners(gateway_url="http://gw.invalid", cell="cell-a", post=gw.post,
                               get=gw.get, claude_bin="/x/claude", research_run=claude,
                               url_get=lambda url, **kw: Live(),
                               url_resolve=lambda host: ["93.184.216.34"])

    class E:
        pass
    e = E()
    e.root, e.seed, e.tok, e.posts, e.factory = tmp_path / "state", seed, tok, posts, factory
    e.tmp = tmp_path

    def run(*argv, gw=None, now=SAT, **kw):
        gw = gw or Gw()
        e.gw = gw
        return cli.run_skunkworks(
            [*argv], post_card=post_card, runners_factory=factory(gw), now=lambda: now,
            state_root=e.root, **kw)
    e.run = run
    e.base = ["--as", "cell-a", "--gateway", "http://gw.invalid", "--token-file", str(tok)]
    return e


def _go(e, *extra, **kw):
    return e.run("run", "--card", "1405", "--persona", "swarph", "--seed-file", str(e.seed),
                 *e.base, *extra, **kw)


def _run_id(capsys):
    out = capsys.readouterr().out
    return json.loads(out)["run_id"]




def test_run_posts_a_typed_status_per_stage_then_ideation_and_architecture(env, capsys):
    assert _go(env, "--json") == 0
    sm = json.loads(capsys.readouterr().out)
    assert sm["status"] == "awaiting_commander" and sm["jobs_used"] == 4
    rid = sm["run_id"]
    statuses = [json.loads(c) for _, k, c in env.posts if k == "status"]
    assert [s["stage"] for s in statuses] == ["ideation", "research", "architecture",
                                              "awaiting_commander"]
    assert all(s["run_id"] == rid and s["type"] == "skunkworks_status" for s in statuses)
    assert [s["jobs_used"] for s in statuses] == [1, 3, 4, 4]
    fyi = [c for _, k, c in env.posts if k == "fyi"]
    assert len(fyi) == 2 and "ideation" in fyi[0] and "architecture" in fyi[1]
    assert all(card == 1405 for card, _, _ in env.posts)


def test_run_refused_without_card_or_seed(env, capsys):
    with pytest.raises(SystemExit):
        env.run("run", "--persona", "swarph", "--seed-file", str(env.seed), *env.base)
    with pytest.raises(SystemExit):
        env.run("run", "--card", "1", "--persona", "swarph", *env.base)
    empty = env.tmp / "empty.txt"
    empty.write_text("  \n")
    assert env.run("run", "--card", "1", "--persona", "swarph", "--seed-file", str(empty),
                   *env.base) == 1
    assert env.run("run", "--card", "1", "--persona", "swarph", "--seed-file",
                   str(env.tmp / "missing"), *env.base) == 1
    assert env.gw.bodies == [] and env.posts == []


def test_run_is_refused_in_the_new_york_session_window(env, capsys):
    est = datetime(2026, 1, 13, 15, 0, tzinfo=timezone.utc)   # 10:00 in New York (EST)
    assert _go(env, now=est) == 1
    assert "cash session" in capsys.readouterr().err
    assert env.gw.bodies == [] and env.posts == []


def test_context_file_goes_to_the_request_and_blocker_to_ideation(env, capsys):
    ctx = env.tmp / "c.md"
    ctx.write_text("CTX-BODY")
    blk = env.tmp / "b.txt"
    blk.write_text("THE BLOCKER")
    assert _go(env, "--context-file", str(ctx), "--blocker-question-file", str(blk), "--json") == 0
    rid = _run_id(capsys)
    req = json.loads((env.root / "cell-a" / "skunkworks" / rid / "request.json").read_text())
    assert req["context_md_paths"] == [str(ctx.resolve())] and req["card_id"] == 1405
    ideation, architect = env.gw.bodies
    assert "THE BLOCKER" in ideation["jobs"][0]["prompt"] and "CTX-BODY" not in ideation["jobs"][0]["prompt"]
    assert "CTX-BODY" in architect["jobs"][0]["prompt"]


def test_long_ideation_is_cut_with_a_note_and_the_path(env, capsys):
    big = {**IDEA, "mechanism": "x" * 250_000}
    assert _go(env, gw=Gw(idea=big)) == 0
    post = next(c for _, k, c in env.posts if k == "fyi" and "ideation" in c[:40])
    assert len(post) <= cli.CARD_POST_LIMIT
    assert post.endswith("ideation.json]") and "cut at 200000" in post and "full text at " in post
    assert cli.CARD_POST_LIMIT == 200_000



def test_card_post_failure_does_not_kill_the_run(env, capsys):
    def boom(*a):
        raise RuntimeError("card down")
    rc = cli.run_skunkworks(
        ["run", "--card", "1", "--persona", "swarph", "--seed-file", str(env.seed), *env.base],
        post_card=boom, runners_factory=env.factory(Gw()), now=lambda: SAT, state_root=env.root)
    assert rc == 0 and "card post failed" in capsys.readouterr().err


def test_a_wrong_lane_answer_makes_the_run_error_exit_nonzero(env, capsys):
    class Wrong(Gw):
        def get(self, url):
            st, d = super().get(url)
            r = json.loads(d["jobs"][0]["result_json"])
            r["served_lane"] = "cursor"
            d["jobs"][0]["result_json"] = json.dumps(r)
            return st, d
    assert _go(env, "--json", gw=Wrong()) == 1
    sm = json.loads(capsys.readouterr().out)
    assert sm["status"] == "error" and "served" in sm["error"]


def test_status_show_stop(env, capsys):
    _go(env, "--json")
    rid = _run_id(capsys)
    assert env.run("status", rid, "--json", *env.base) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "awaiting_commander"
    assert env.run("status", rid, *env.base) == 0
    assert rid in capsys.readouterr().out
    assert env.run("show", rid, "--stage", "ideation", *env.base) == 0
    assert json.loads(capsys.readouterr().out)["concept_name"] == "Stub"
    assert env.run("show", rid, *env.base) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"ideation", "research", "architecture"}
    assert env.run("stop", rid, "--json", *env.base) == 0
    assert (env.root / "cell-a" / "skunkworks" / rid / "STOP").is_file()
    assert env.run("status", "../etc", *env.base) == 1
    assert env.run("status", "sk_1_deadbeef", *env.base) == 1


def test_a_dead_run_reads_as_error_in_status(env, capsys):
    rdir = env.root / "cell-a" / "skunkworks"
    from swarph_mesh.skunkworks.artifacts import persist
    from swarph_mesh.skunkworks.state import RunState, RunStatus
    st = RunState.new("cell-a", "swarph", "s", 1)
    st.pid = 2**22 + 12345  # not a live pid
    st.transition(RunStatus.RESEARCHING)
    persist(st, str(rdir))
    assert env.run("status", st.run_id, "--json", *env.base) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def _quote(env, text="Approved, go ahead - the commander"):
    q = env.tmp / "quote.txt"
    q.write_text(text)
    return str(q)


def test_decide_refusal_prints_the_gate_dict_and_exits_nonzero(env, capsys):
    _go(env, "--json", gw=Gw(arch_verdict="NEEDS_REFINEMENT"))
    rid = _run_id(capsys)
    env.posts.clear()
    assert env.run("decide", rid, "APPROVED", "--quote-file", _quote(env), *env.base) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "architect_verdict_gate" and out["arch_verdict"] == "NEEDS_REFINEMENT"
    assert env.posts == []
    assert not (env.root / "cell-a" / "skunkworks" / rid / "assessment.json").exists()


def test_decide_approved_records_and_posts_the_commanders_words(env, capsys):
    _go(env, "--json")
    rid = _run_id(capsys)
    env.posts.clear()
    assert env.run("decide", rid, "APPROVED", "--quote-file", _quote(env), "--json", *env.base) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "approved"
    (card, kind, content), = env.posts
    assert card == 1405 and kind == "status"
    assert "Approved, go ahead - the commander" in content and rid in content
    a = json.loads((env.root / "cell-a" / "skunkworks" / rid / "assessment.json").read_text())
    assert a["commander_words"] == "Approved, go ahead - the commander"


def test_decide_requires_a_quote_and_a_non_empty_one(env, capsys):
    _go(env, "--json")
    rid = _run_id(capsys)
    with pytest.raises(SystemExit):
        env.run("decide", rid, "REVISE", *env.base)
    assert env.run("decide", rid, "REVISE", "--quote-file", _quote(env, " \n"), *env.base) == 1
    assert env.run("decide", rid, "REVISE", "--quote-file", str(env.tmp / "nope"), *env.base) == 1
    assert env.run("decide", rid, "MAYBE", "--quote-file", _quote(env), *env.base) == 1


