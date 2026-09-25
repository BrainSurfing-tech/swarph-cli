"""Card #957: authenticated HTTP bench arms. All eight tests are offline."""
import json
import os

import pytest

from swarph_cli.bench.backends import HttpBackend, RuleBackend, TypedHttpBackend
from swarph_cli.bench.runner import ModelSpec, parse_models, preflight, run_pack

TOKEN = "jev-test-token-not-a-real-key"


def _prompt():
    return json.dumps({"state": {"n": 1}, "questions": {"answer": {"type": "choice"}}})


def _pack(kind="typed", egress=None, n=1):
    pack = {
        "theme": "t",
        "system": "s",
        "request_kind": kind,
        "tasks": [
            {"id": f"t{i}", "type": "categorical", "prompt": _prompt() if kind == "typed" else "hi",
             "expected": "YES"}
            for i in range(n)
        ],
    }
    if egress:
        pack["egress"] = egress
    return pack


def _arm(**kw):
    base = dict(
        name="jev", kind="typed", base_url="https://jevtypesafeai.com/api",
        path="/v1/decide",
        auth={"env": "JEV_API_KEY", "header": "Authorization", "scheme": "Bearer"},
        price={"in_per_mtok": 0.42, "out_per_mtok": 0, "source": "example"},
        egress="external",
    )
    base.update(kw)
    return HttpBackend(**base)


def _spec(arm):
    return ModelSpec(id="jev", backend="provider", label="jev", provider="jev", arm=arm)


def test_1_unset_credential_drops_before_any_request(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    arm = _arm()
    runnable, warnings = preflight([_spec(arm)], {}, pack=_pack())
    assert runnable == []
    assert any("JEV_API_KEY (provider jev)" in w for w in warnings)
    assert arm.calls == 0


def test_2_error_body_does_not_leak(tmp_path, caplog, capsys, monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    body = f"nope {TOKEN} and Bearer {TOKEN}".encode()

    def transport(url, headers, raw):
        return 401, "Unauthorized", body

    arm = _arm(transport=transport)
    result = run_pack(
        [_spec(arm)], _pack(), {}, ledger_path=str(tmp_path / "ledger.json"))
    row = result["detail"]["jev"][0]
    assert row["error"] == "HTTP 401 Unauthorized"
    blob = json.dumps(result) + (tmp_path / "ledger.json").read_text()
    blob += caplog.text + "".join(capsys.readouterr())
    assert TOKEN not in blob
    assert TOKEN not in (row["error"] or "")


def test_3_token_is_only_in_the_declared_header(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    seen = {}

    def transport(url, headers, raw):
        seen["url"] = url
        seen["headers"] = dict(headers)
        return 200, "OK", json.dumps({
            "answers": {"answer": {"choice": "YES"}},
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }).encode()

    arm = _arm(transport=transport)
    run_pack([_spec(arm)], _pack(), {})
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in seen["url"]
    assert TOKEN not in seen["headers"].get("Content-Type", "")


def test_4_kind_mismatch_refuses_both_ways(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    typed = _arm()
    semantic = _arm(kind="semantic", path="/v1/chat/completions")
    for pack, arm in ((_pack("typed"), semantic), (_pack("semantic"), typed)):
        arm.calls = 0
        runnable, warnings = preflight([_spec(arm)], {}, pack=pack)
        assert runnable == []
        assert any(w.startswith("kind mismatch:") for w in warnings)
        assert arm.calls == 0


def test_5_on_box_only_refuses_external_until_named(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)

    def transport(url, headers, raw):
        return 200, "OK", json.dumps({
            "answers": {"answer": {"choice": "YES"}},
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }).encode()

    arm = _arm(transport=transport)
    pack = _pack(egress="on_box_only")
    runnable, warnings = preflight([_spec(arm)], {}, pack=pack)
    assert runnable == []
    assert any("on_box_only" in w and "--allow-egress jev" in w for w in warnings)
    assert arm.calls == 0
    runnable, _ = preflight([_spec(arm)], {}, pack=pack, allow_egress={"jev"})
    assert runnable
    result = run_pack(runnable, pack, {}, allow_egress={"jev"})
    assert result["egress_overrides"][0]["provider"] == "jev"
    assert arm.calls == 1
    local = TypedHttpBackend()
    spec = parse_models("typed-http:http://127.0.0.1:9")[0]
    runnable, warnings = preflight([spec], {"typed-http": local}, pack=pack)
    assert runnable and not any("on_box_only" in w for w in warnings)


def test_6_spend_cap_stops_before_the_fifth_dispatch(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)

    def transport(url, headers, raw):
        return 200, "OK", json.dumps({
            "answers": {"answer": {"choice": "YES"}},
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
        }).encode()

    arm = _arm(transport=transport, price={"in_per_mtok": 0.03, "out_per_mtok": 0, "source": "fake"})
    result = run_pack([_spec(arm)], _pack(n=6), {}, max_usd=0.10)
    assert arm.calls == 4
    rows = result["detail"]["jev"]
    assert sum(1 for r in rows if r.get("not_run") == "spend_cap") == 2
    assert result["partial"] == "spend_cap"
    assert result["exit_code"] == 1
    assert result["board"][0]["partial"] == "spend_cap"


def test_7_unpriced_arm_is_refused_under_a_cap(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    arm = _arm(price=None)
    runnable, warnings = preflight([_spec(arm)], {}, pack=_pack(), max_usd=0.10)
    assert runnable == []
    assert any("unpriced" in w for w in warnings)
    runnable, _ = preflight(
        [_spec(arm)], {}, pack=_pack(), max_usd=0.10, allow_unpriced={"jev"})
    assert runnable
    assert arm.calls == 0


def test_8_typed_http_and_rule_arms_still_parse():
    specs = parse_models("typed-http:http://127.0.0.1:9,rule:swarph_cli.bench.rules:s1_code_question")
    assert [s.backend for s in specs] == ["typed-http", "rule"]
    assert TypedHttpBackend().missing_creds() == []
    assert RuleBackend().missing_creds() == []
    assert TypedHttpBackend.KIND == "typed"
