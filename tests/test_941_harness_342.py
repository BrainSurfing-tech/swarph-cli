"""Card #941 build #342: rule arm sees state, noul uses 0.50, three adapters."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from swarph_cli.bench.backends import RuleBackend, TypedHttpBackend
from swarph_cli.bench.quality import d_categorical, parse_answer
from swarph_cli.bench.rules import (
    s1_code_question,
    s2_claim_vs_mention,
    s3_dm_triage,
)


def test_rule_unwraps_state_and_ignores_coding_words_in_the_question():
    prompt = json.dumps({
        "state": {"text": "thanks, see you tomorrow"},
        "questions": {
            "answer": {
                "type": "noul",
                "instructions": "does this ask about a function?",
            }
        },
    })
    result = RuleBackend().generate(
        "swarph_cli.bench.rules:s1_code_question", prompt, ""
    )
    assert result.error is None, result.error
    assert parse_answer(result.text) == "NO"
    assert s1_code_question({"text": "where is the function"}) == "YES"


def test_s2_claim_vs_mention_is_the_current_verify_rule():
    assert s2_claim_vs_mention(
        {"text": "binds 127.0.0.1:8099", "asserted": None, "kind": "path"}
    ) == "MENTION"
    assert s2_claim_vs_mention({
        "text": "SUPERSEDED binds 127.0.0.1:8099",
        "asserted": "127.0.0.1:8099",
    }) == "MENTION"
    assert s2_claim_vs_mention({
        "text": "on 2026-09-21 binds 127.0.0.1:8099",
        "asserted": "127.0.0.1:8099",
    }) == "MENTION"
    assert s2_claim_vs_mention({
        "text": "binds 127.0.0.1:8099",
        "asserted": "127.0.0.1:8099",
    }) == "CLAIM"


def test_s3_dm_triage_is_the_current_filter():
    assert s3_dm_triage({"dm": {"kind": "fyi", "content": "hello"}}) == "ROUTINE"
    assert s3_dm_triage({
        "dm": {"id": 1, "from_node": "lab-ovh", "kind": "fyi", "content": "receipt: ok"},
    }) == "ROUTINE"
    woke = s3_dm_triage({
        "dm": {"id": 2, "from_node": "lab-ovh", "kind": "question", "content": "take this"},
    })
    assert woke == "ACT"
    assert d_categorical(woke, "ACT") == 0.0


class _NoulStub(BaseHTTPRequestHandler):
    p_true = 0.51

    def do_POST(self):
        payload = {
            "answers": {"answer": {"type": "noul", "noul": _NoulStub.p_true}},
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


def test_typed_http_thresholds_noul_at_0_50_and_keeps_p_true():
    template = json.dumps({
        "state": {"text": "thanks"},
        "questions": {
            "answer": {
                "type": "noul",
                "instructions": "is this a code question?",
                "labels": {"true": "YES", "false": "NO"},
            }
        },
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoulStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        backend = TypedHttpBackend()
        seen = {}
        for p_true, label in ((0.49, "NO"), (0.51, "YES"), (0.50, "YES")):
            _NoulStub.p_true = p_true
            result = backend.generate(url, template, "")
            assert result.error is None, result.error
            body = json.loads(result.text)
            assert body["answer"] == label
            assert body["p_true"] == p_true
            assert d_categorical(parse_answer(result.text), label) == 0.0
            seen[p_true] = label
    finally:
        server.shutdown()
        server.server_close()
    assert seen == {0.49: "NO", 0.51: "YES", 0.50: "YES"}
