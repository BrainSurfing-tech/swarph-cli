"""Card #941 step 1: rule: and typed-http: bench backends.

A rule backend returns the callable's label. A typed-http backend against a
stub /v1/systemone returns the typed answer and its latency. d_categorical
scores both. Neither lane asks for a credential. bench validate still passes
on the existing arithmetic pack.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from swarph_cli.bench.backends import RuleBackend, TypedHttpBackend
from swarph_cli.bench.quality import d_categorical, parse_answer
from swarph_cli.bench.runner import parse_models
from swarph_cli.commands import bench


def yes_label(prompt: str, system: str = "") -> str:
    return "YES"


def test_parse_models_keeps_rule_and_typed_http_payloads():
    specs = parse_models(
        "gemini-2.5-flash:subscription:fast-gem,"
        "rule:tests.test_941_bench_backends:yes_label,"
        "typed-http:http://127.0.0.1:9"
    )
    assert [(s.id, s.backend) for s in specs] == [
        ("gemini-2.5-flash", "subscription"),
        ("tests.test_941_bench_backends:yes_label", "rule"),
        ("http://127.0.0.1:9", "typed-http"),
    ]


def test_rule_backend_returns_callable_label_and_scores():
    backend = RuleBackend()
    assert backend.missing_creds() == []
    result = backend.generate(
        "tests.test_941_bench_backends:yes_label", "pick one", "be brief"
    )
    assert result.error is None
    assert result.latency_s >= 0
    label = parse_answer(result.text)
    assert label == "YES"
    assert d_categorical(label, "YES") == 0.0


class _Stub(BaseHTTPRequestHandler):
    seen_auth = False

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("Authorization"):
            _Stub.seen_auth = True
        assert self.path == "/v1/systemone"
        assert "questions" in body
        payload = {
            "answers": {
                "answer": {"type": "choice", "choice": "CLAIM"},
            },
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


def test_typed_http_against_stub_returns_answer_and_latency():
    _Stub.seen_auth = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        backend = TypedHttpBackend()
        assert backend.missing_creds() == []
        result = backend.generate(f"http://127.0.0.1:{port}", "is it a claim?", "")
    finally:
        server.shutdown()
        server.server_close()
    assert result.error is None, result.error
    assert result.latency_s > 0
    assert not _Stub.seen_auth
    label = parse_answer(result.text)
    assert label == "CLAIM"
    assert d_categorical(label, "CLAIM") == 0.0
    assert d_categorical(label, "YES") == 1.0


def test_bench_validate_existing_pack_still_passes(capsys):
    rc = bench.run_bench(["validate", "packs/arithmetic_demo.json"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out
