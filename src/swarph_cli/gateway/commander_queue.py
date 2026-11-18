"""Commander queue's canonical wire data (card #1630, Appendix A)."""
import hashlib
import unicodedata

import rfc8785


def canonical_bytes(request_id: int, kind: str, params: dict) -> bytes:
    """Hash the stored values as-is: no Unicode normalization or field insertion."""
    return rfc8785.dumps({"id": request_id, "type": kind, "params": params})


def digest(request_id: int, kind: str, params: dict) -> str:
    return hashlib.sha256(canonical_bytes(request_id, kind, params)).hexdigest()


def text_ok(value: object, maximum: int, *, nonempty: bool = True) -> bool:
    return (isinstance(value, str) and len(value) <= maximum
            and (not nonempty or bool(value.strip()))
            and all(ch.isprintable() and unicodedata.category(ch) != "Cf" for ch in value))


def decision_fits(params: dict) -> bool:
    options = params.get("options", [])
    return (text_ok(params.get("question"), 360) and isinstance(options, list)
            and len(options) <= 6 and all(text_ok(option, 40) for option in options)
            and isinstance(params.get("needs_sso", False), bool))


LEGACY_SUMMARY = "This question exceeds the cockpit limits: answer it in the Command Center."


def summary(request_id: int, kind: str, params: dict) -> list[str]:
    if kind == "decision":
        if not decision_fits(params):
            return [LEGACY_SUMMARY]
        question = params["question"]
        return ([f"Question #{request_id}:"]
                + [question[index:index + 120] for index in range(0, len(question), 120)]
                + ["Option: " + option for option in params.get("options", [])]
                + (["Needs the SSO session: the cockpit cannot answer this one."]
                   if params.get("needs_sso", False) else ["The cockpit may answer this question."]))
    if kind == "merge_pr":
        lines = [f"Merge PR #{params.get('pr')} in {params.get('repo')}",
                 f"at head {params.get('head_sha')}"]
    elif kind == "release_finish":
        lines = [f"Finish release {params.get('version')}",
                 f"PR #{params.get('pr')} at head {params.get('head_sha')}"]
    else:
        lines = [f"Request #{request_id}: review in the Command Center."]
    if not all(text_ok(line, 120) for line in lines):
        return [f"Request #{request_id} exceeds the cockpit limits: review in the Command Center."]
    return lines
