"""#656: the swarph-OWNED SLM client, so ENRICH can run on an installed wheel.

Before this, enrich required `workers.slm_client` from a private hedge-fund-mcp
layout. Every wheel lacked it, so ENRICH had never run outside one box.
"""
import json
import os
import urllib.request

import pytest

from swarph_cli.dreaming import enrich as E
from swarph_cli.dreaming import slm


def _clearenv(mp):
    for k in (slm.ENDPOINT_ENV, slm.MODEL_ENV, slm.KEY_ENV, slm.TIMEOUT_ENV):
        mp.delenv(k, raising=False)


# ---- #578: NO HOST DEFAULT -------------------------------------------------

def test_no_endpoint_configured_means_unconfigured(monkeypatch):
    _clearenv(monkeypatch)
    assert slm.configured() is None
    assert slm.available() is False


def test_endpoint_without_model_is_still_unconfigured(monkeypatch):
    """Half-configured must not fall back to an implicit model — that is the
    same expiry problem as a baked host, one level down."""
    _clearenv(monkeypatch)
    monkeypatch.setenv(slm.ENDPOINT_ENV, "http://example.invalid:11434")
    assert slm.configured() is None


def test_constructing_without_config_refuses_and_names_578(monkeypatch):
    _clearenv(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        slm.SLMClient()
    assert "#578" in str(e.value)


def test_no_hardcoded_address_in_the_module():
    """CAN-FAIL: putting any dotted-quad or :11434 back into the source fails here."""
    import re
    src = (__import__("pathlib").Path(slm.__file__)).read_text()
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    code = code.split('"""')[0] + '"""'.join(code.split('"""')[2:])  # drop the docstring
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", code), "hardcoded IP in code (#578)"
    assert ":11434" not in code, "hardcoded ollama port in code (#578)"


# ---- the generate contract -------------------------------------------------

def test_generate_posts_openai_shape_and_returns_raw_content(monkeypatch):
    _clearenv(monkeypatch)
    monkeypatch.setenv(slm.ENDPOINT_ENV, "http://slm.test:1234/")
    monkeypatch.setenv(slm.MODEL_ENV, "tiny:1b")
    seen = {}

    class _Resp:
        status = 200
        def read(self): return json.dumps(
            {"choices": [{"message": {"content": '[{"file":"a","link":"b"}]'}}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = slm.SLMClient().generate("hello")

    # trailing slash on the endpoint must not produce a doubled path
    assert seen["url"] == "http://slm.test:1234/v1/chat/completions"
    assert seen["body"]["model"] == "tiny:1b"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert seen["body"]["stream"] is False
    # RAW string, not parsed: enrich needs the whole JSON array, and a dict-coercing
    # helper would keep only the first element.
    assert out == '[{"file":"a","link":"b"}]'


def test_api_key_sent_only_when_set(monkeypatch):
    _clearenv(monkeypatch)
    monkeypatch.setenv(slm.ENDPOINT_ENV, "http://slm.test:1234")
    monkeypatch.setenv(slm.MODEL_ENV, "tiny:1b")
    hdrs = {}

    class _Resp:
        status = 200
        def read(self): return json.dumps(
            {"choices": [{"message": {"content": "x"}}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=None):
        hdrs.clear(); hdrs.update(req.headers); return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slm.SLMClient().generate("p")
    assert not any(k.lower() == "authorization" for k in hdrs), "local Ollama needs no key"

    monkeypatch.setenv(slm.KEY_ENV, "sk-test")
    slm.SLMClient().generate("p")
    assert any(k.lower() == "authorization" for k in hdrs), "hosted API needs the key"


def test_unexpected_payload_raises_rather_than_returning_empty(monkeypatch):
    """An empty string would read to enrich as 'the model proposed nothing' —
    a DIFFERENT state from 'the endpoint answered something else'."""
    _clearenv(monkeypatch)
    monkeypatch.setenv(slm.ENDPOINT_ENV, "http://slm.test:1234")
    monkeypatch.setenv(slm.MODEL_ENV, "tiny:1b")

    class _Resp:
        status = 200
        def read(self): return json.dumps({"error": "nope"}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda r, timeout=None: _Resp())
    with pytest.raises(KeyError):
        slm.SLMClient().generate("p")


# ---- enrich wiring ---------------------------------------------------------

def test_enrich_reports_unavailable_when_nothing_is_configured(monkeypatch):
    _clearenv(monkeypatch)
    # must never raise, whichever layout this box has
    assert E.slm_client_available() in (True, False)
    if _no_private_workers():
        assert E.slm_client_available() is False, \
            "no workers, no configured endpoint -> must report unavailable, not guess"


def test_enrich_uses_the_owned_client_when_workers_is_absent(monkeypatch):
    """The point of the card: an installed wheel with no `workers` must still enrich."""
    if not _no_private_workers():
        pytest.skip("this box has the private workers layout")
    _clearenv(monkeypatch)
    monkeypatch.setenv(slm.ENDPOINT_ENV, "http://slm.test:1234")
    monkeypatch.setenv(slm.MODEL_ENV, "tiny:1b")
    monkeypatch.setattr(slm, "available", lambda: True)
    assert E.slm_client_available() is True
    assert isinstance(E._client(), slm.SLMClient)


def test_skip_string_is_still_the_contract():
    assert E.ENRICH_SKIPPED_NO_SLM == "enrich skipped: no SLM client"


def _no_private_workers() -> bool:
    try:
        import workers.slm_client  # noqa: F401
        return False
    except ImportError:
        return True
