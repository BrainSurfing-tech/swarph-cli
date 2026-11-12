"""Device-code onboarding without a human-mediated token handoff (card #1582)."""
import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.parse import urlsplit

from ._display import sanitize_terminal

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# Gateway server.PEER_NAME_RE; legacy witness names are validated separately.
_NEW_PEER = re.compile(r"[a-z][a-z0-9-]{0,62}[a-z0-9]")
_CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
# RFC6598 shared address space, not a configured gateway host/default.
_TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_QUESTIONS = (
    "How will you handle routine AI-to-AI peer DMs, and stop at privilege boundaries?",
    "How will you keep your canonical name and flag a DM addressed to the wrong identity?",
    "How will you disclose mistakes and assumptions to the peers affected?",
    "What counts as a mesh secret, and how will you keep it out of mesh messages?",
)


class JoinError(Exception):
    pass


def _gateway(value, allow_unsafe=False):
    try:
        if any(ord(ch) <= 32 for ch in value):
            raise ValueError
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError
        url.port
        host = url.hostname.rstrip(".").lower()
        tailnet = host.endswith(".ts.net")
        try:
            tailnet = tailnet or ipaddress.ip_address(host) in _TAILNET_CGNAT
        except ValueError:
            pass
        if url.scheme == "http" and not tailnet and not allow_unsafe:
            raise JoinError("non-tailnet HTTP requires --allow-unsafe-http; HTTPS is preferred")
        return url, url.scheme == "http" and tailnet
    except (ValueError, TypeError):
        raise JoinError("invalid gateway URL") from None


def _request(url, method, path, body=None, token=None, timeout=15):
    connection = (http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection)(
        url.hostname, url.port, timeout=timeout)
    try:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise JoinError("gateway redirect refused")
        raw = response.read(16385)
        if len(raw) > 16384:
            raise JoinError("gateway response too large")
        payload = json.loads(raw)
        if type(payload) is not dict:
            raise JoinError("invalid gateway response")
        retry = response.getheader("Retry-After", "5")
        retry = max(5, min(int(retry), 900)) if retry.isdecimal() else 5
        return response.status, payload, retry
    except (OSError, ValueError, http.client.HTTPException):
        raise JoinError("gateway request failed; check the URL and tailnet connectivity; do not blindly retry a token pickup") from None
    finally:
        connection.close()


def _exclusive(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    try:
        os.fchmod(fd, 0o600)
        return fd, os.fstat(fd)
    except Exception:
        os.close(fd)
        _remove_ours(path, info)
        raise


def _write(fd, value):
    with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
        stream.write(value)
        stream.flush()
        os.fsync(fd)


def _remove_ours(path, info):
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            path.unlink()
    except FileNotFoundError:
        pass


def run_join(argv):
    parser = argparse.ArgumentParser(prog="swarph join", description=__doc__)
    parser.add_argument("gateway")
    parser.add_argument("--name", required=True)
    parser.add_argument("--allow-unsafe-http", action="store_true", help="UNSAFE: permit non-tailnet cleartext HTTP")
    args = parser.parse_args(argv)
    reservations = []
    token_saved = config_saved = False
    try:
        if os.name == "nt":
            raise JoinError("native Windows credential storage is unsupported; use Linux or WSL")
        if not _NEW_PEER.fullmatch(args.name):
            raise JoinError("invalid peer name")
        url, tailnet = _gateway(args.gateway, args.allow_unsafe_http)
        if tailnet:
            print("Transport: the tailnet's encrypted tunnel, not TLS.")
        folder = Path.home() / ".config" / "swarph"
        for parent in (Path.home(), folder.parent, folder):
            if parent.is_symlink():
                raise JoinError("symlinked credential directory refused")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.stat().st_uid != os.geteuid():
            raise JoinError("credential directory must belong to this user")
        folder.chmod(0o700)
        token_path = folder / f"{args.name}.peer_token"
        config_path = folder / f"{args.name}.join.json"
        for path in (token_path, config_path):
            fd, info = _exclusive(path)
            reservations.append((path, fd, info))
        status, created, _ = _request(url, "POST", "/join/requests", {"name": args.name})
        if status != 201:
            raise JoinError("join request refused (HTTP " + str(status) + ")")
        device = created.get("device_code")
        code = created.get("user_code")
        if (not isinstance(device, str) or not _SECRET.fullmatch(device)
                or not isinstance(code, str) or not _CODE.fullmatch(code)
                or type(created.get("expires_in")) is not int or created["expires_in"] != 900
                or type(created.get("interval")) is not int or created["interval"] != 5):
            raise JoinError("invalid join response")
        print(f"Code: {code} (expires in 15 minutes). Ask the operator to run: swarph mesh approve {code} --gateway {shlex.quote(args.gateway)} --witness <peer>")
        deadline = time.monotonic() + 900
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise JoinError("join request expired")
            status, picked, retry = _request(url, "POST", "/join/token", {"device_code": device}, timeout=min(15, left))
            if status == 200:
                break
            if ((status == 400 and picked.get("error") == "authorization_pending")
                    or (status == 429 and picked.get("error") == "slow_down")):
                time.sleep(min(retry, max(0, deadline - time.monotonic())))
                continue
            error = picked.get("error")
            if error not in {"access_denied", "expired_token", "already_redeemed", "name_unavailable"}:
                error = "pickup_failed"
            raise JoinError(error)
        token, witness = picked.get("peer_token"), picked.get("witness")
        if (not isinstance(token, str) or not _SECRET.fullmatch(token)
                or picked.get("name") != args.name or picked.get("registered_unratified") is not True
                or not isinstance(witness, str) or not _NAME.fullmatch(witness)):
            raise JoinError("invalid token pickup response; operator recovery may be needed")
        _write(reservations[0][1], token + "\n")
        token_saved = True
        status, identity, _ = _request(url, "GET", "/whoami", token=token)
        if status != 200 or identity.get("peer") != args.name:
            raise JoinError("authenticated identity check failed; the private credential file is retained")
        _write(reservations[1][1], json.dumps({"name": args.name, "gateway": args.gateway.rstrip("/"),
                                             "token_file": str(token_path)}) + "\n")
        config_saved = True
        print("Credential saved privately. Ratification is still pending; answer in your own words:")
        for question in _QUESTIONS:
            print(question)
        if sys.stdin.isatty():
            try:
                answers = [input(f"Answer {i + 1}: ").strip() for i in range(4)]
            except EOFError:
                raise JoinError("handshake incomplete; no answers sent, credential remains saved") from None
            if not all(answers):
                raise JoinError("handshake incomplete; no answers sent")
            from swarph_cli.redact import redact_text
            content = "Handshake for " + args.name + "\n" + "\n".join(
                question + "\n" + answer for question, answer in zip(_QUESTIONS, answers))
            content = redact_text(content.replace(token, "<REDACTED>").replace(device, "<REDACTED>"))
            status, _, _ = _request(url, "POST", "/messages", {
                "from_node": args.name, "to_node": witness, "kind": "status", "content": content}, token)
            if not 200 <= status < 300:
                raise JoinError("handshake send failed; credential remains saved, ratification pending")
            print("Own-words handshake sent to " + witness + "; the witness must ratify separately.")
        else:
            print("No interactive answers were supplied; no handshake was sent. Send your own answers to " + witness + ".")
        print(f"Use explicit identity: swarph mesh peers --as {args.name} --gateway {shlex.quote(args.gateway.rstrip('/'))} --token-file {shlex.quote(str(token_path))}")
        return 0
    except (JoinError, OSError, ValueError, KeyboardInterrupt) as exc:
        print("swarph join: " + (str(exc) if isinstance(exc, JoinError) else "local storage failure or cancellation; do not retry blindly; ask the operator to check any in-flight pickup"), file=sys.stderr)
        return 1
    finally:
        for index, (path, fd, info) in enumerate(reservations):
            os.close(fd)
            if not (token_saved if index == 0 else config_saved):
                _remove_ours(path, info)


def approve_join(args):
    from swarph_cli import tokens
    try:
        code = args.code.upper()
        if not _CODE.fullmatch(code) or (args.witness and not _NAME.fullmatch(args.witness)):
            raise JoinError("invalid approval code or witness")
        url, tailnet = _gateway(args.gateway or "", args.allow_unsafe_http)
        if tailnet:
            print("Transport: the tailnet's encrypted tunnel, not TLS.")
        resolved = tokens.resolve_token(None, args.token_file, env_keys=("MESH_GATEWAY_TOKEN",),
                                        identity_is_explicit=False, allow_peer_token=False, secrets_path=None)
        if resolved is None or not resolved.token:
            raise JoinError("operator credential required; use --token-file")
        status, preview, _ = _request(url, "GET", f"/join/requests/{code}", token=resolved.token)
        if status != 200 or preview.get("state") != "pending":
            raise JoinError("approval preview refused or request is no longer pending")
        if (not isinstance(preview.get("name"), str) or not _NEW_PEER.fullmatch(preview["name"])
                or not isinstance(preview.get("source"), str) or not 1 <= len(preview["source"]) <= 256
                or type(preview.get("expires_at")) not in (int, float)
                or not 0 < preview["expires_at"] < 1e12):
            raise JoinError("invalid approval preview; nothing changed")
        for field in ("name", "source", "expires_at"):
            print(field + ": " + sanitize_terminal(str(preview.get(field, "unknown"))).replace("\n", " ").replace("\r", " "))
        decision = "refuse" if args.refuse else "approve"
        if input("Confirm this code came from the intended client; type yes to " + decision + ": ").strip() != "yes":
            raise JoinError("decision cancelled; nothing changed")
        body = {"decision": decision}
        if not args.refuse:
            body["witness"] = args.witness
        status, result, _ = _request(url, "POST", f"/join/requests/{code}/decision", body, resolved.token)
        expected = "refused" if args.refuse else "approved"
        if status != 200 or result.get("state") != expected:
            raise JoinError("decision refused; re-read the request before retrying")
        print("Request " + expected + ". No newcomer credential was received by this operator.")
        return 0
    except (JoinError, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        print("swarph mesh approve: " + (str(exc) if isinstance(exc, JoinError) else "credential read failed or confirmation cancelled"), file=sys.stderr)
        return 1
