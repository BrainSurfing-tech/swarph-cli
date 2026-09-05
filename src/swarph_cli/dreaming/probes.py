"""The read-only verb table (GC3). Every probe here is a look, never a change.

GC4: a kind may have MORE THAN ONE authoritative surface. `unit_bind` has
two -- what the unit declares and what the running container actually did. On
2026-09-03 those disagreed on this box (unit: -p 10.0.0.1:8081:8080;
container: HostIp empty = all interfaces) because the container was
hand-started and outlived its unit. A one-surface probe would have "corrected"
a memory that is accurate about the designed state. Disagreement is an
INFRASTRUCTURE finding.

GC4f: every surface carries mode `declared` or `observed`. The name stays a
string so Task 4 can key on it; a dict-shaped surface is unhashable and is
the TypeError that crashed every unit_state candidate in pre-build review.

GC3d: a network-reaching surface records elapsed time. An absence under
SCOPE_FLOOR_S is about the instrument (`not_my_scope`), not the target.
"""
from __future__ import annotations
import json, re, subprocess, time, urllib.request

TIMEOUT = 10
HTTP_TIMEOUT = 5
# 1ms. This box curling its own public address: 50-85µs (instrument).
# gpu-wsl remote refusal: 46ms. Tailnet answer: 20ms. ~400x apart.
SCOPE_FLOOR_S = 0.001

# GC3's table, verbatim and ENFORCED HERE. Every probe funnels through _run,
# so this set is the guarantee -- not the lint at the end of this task.
# (a peer, DM 30235: a denylist grep can only screen verbs its author
# already thought of. `docker exec` passes such a grep, sits next to
# `docker inspect`, reads like an inspection, and runs anything in the
# container. So does `docker cp`, `systemctl reload|mask`, `curl -X POST`.)
_ALLOWED = {("systemctl", "is-active"), ("systemctl", "is-enabled"),
            ("systemctl", "cat"), ("systemctl", "show"),
            ("docker", "inspect"), ("ss", "-lnt")}


def _run(argv: list[str]) -> str:
    if tuple(argv[:2]) not in _ALLOWED:
        raise RuntimeError("verb not in the GC3 table: %r" % (argv[:2],))
    # No shell, ever. argv elements come only from candidates that passed
    # candidates.SAFE_REF.
    r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    return r.stdout


def _path(c):
    """>>> `Path.exists()` CANNOT TELL "MISSING" FROM "NOT READABLE BY ME". <<<

    It returns False for BOTH, so a path under a directory this uid cannot
    traverse is reported `absent` and scored `disagree` — a confident WRONG
    correction against a memory that is right.

    MEASURED 2026-09-04 on the first real-corpus run:
        /home/swarphbrain/.gbrain/brain.pglite
            Path.exists() as ubuntu -> False   ("absent", filed as a finding)
            sudo test -e            -> EXISTS

    `os.stat` separates them: PermissionError vs FileNotFoundError. A
    permission failure is NOT an observation about the artifact, it is an
    observation about THIS PROBE'S REACH — so it returns an error and
    `_compare` routes it to unprobeable/not_my_scope, never to disagree.
    This is GC3c's scope rule (written for `listen_addr`) finally applied to
    the filesystem, where the boundary is a uid instead of a host.
    """
    import os
    try:
        os.stat(c["ref"])
        return [{"surface": "filesystem", "mode": "observed",
                 "observed": "exists", "error": None}]
    except FileNotFoundError:
        return [{"surface": "filesystem", "mode": "observed",
                 "observed": "absent", "error": None}]
    except PermissionError:
        # NOT a fact about the path. A fact about this uid.
        return [{"surface": "filesystem", "mode": "observed",
                 "observed": None, "error": "not_my_scope"}]
    except OSError as e:
        return [{"surface": "filesystem", "mode": "observed",
                 "observed": None, "error": "probe_error:%s" % e.__class__.__name__}]


def _pkg_version(c):
    from importlib.metadata import version, PackageNotFoundError
    try:
        return [{"surface": "dist-metadata", "mode": "observed",
                 "observed": version(c["ref"]), "error": None}]
    except PackageNotFoundError:
        return [{"surface": "dist-metadata", "mode": "observed",
                 "observed": None, "error": "not installed"}]


def _unit_state(c):
    """TWO FIELDS, RETURNED SEPARATELY. ActiveState and UnitFileState are not one
    value and must not be joined into one string.

    THE DEFECT THIS REPLACES, traced 2026-09-03 through the previous version:
    the probe returned "inactive/enabled" and `_agrees` did a SUBSTRING test.
    For the memory "mesh-gateway.service -> inactive (but enabled)" the
    extractor kept only "inactive", and `"inactive" in "inactive/enabled"` is
    True -- but so is `"inactive" in "inactive/disabled"`. >>> THE VERDICT READ
    `agree` WHETHER OR NOT THE CLAIM WAS TRUE ON THE HALF THAT CARRIED THE
    FINDING. <<< inactive+enabled is exactly the pair that identifies a
    hand-started service outliving its unit -- the thing that cost this estate
    a live 0.0.0.0 exposure on 8081 -- so the dropped half was the whole point.

    AND THE SUBSTRING BUG WAS WORSE THAN THAT CASE, which is the reason this
    must never be "simplified" back: >>> `"active" in "inactive"` IS TRUE. <<<
    So the old comparison ALSO returned `agree` for a memory asserting a unit
    is ACTIVE against a probe that found it INACTIVE -- a dead service
    confirming a memory that says it is running, on the PRIMARY purpose of this
    kind, not an edge case. (a peer, 2026-09-03, checking whether the
    fix needed extending. It does not: comparing a dict field-wise kills this by
    CONSTRUCTION rather than by enumeration, which is why the fix is structural
    and not a patch for the specimen that motivated it.)
    """
    active = _run(["systemctl", "is-active", c["ref"]]).strip()
    enabled = _run(["systemctl", "is-enabled", c["ref"]]).strip()

    # >>> A UNIT THAT DOES NOT EXIST IS ABSENT, NOT INACTIVE-AND-NOT-FOUND. <<<
    # a peer, #656 seat-A Part 2 GC3a: `_run` discards the return code,
    # `is-enabled` on a nonexistent unit prints `not-found` with rc 4, and the
    # result is still a DICT -- which verify.py treats as never-absent. So a memory
    # naming a unit that DOES NOT EXIST read `agree`. A false GREEN, which is worse
    # than a false accusation: nothing ever looks at it again.
    #
    # `not-found` is a clean absence marker, not an overloaded state. a peer
    # swept 120 live units before this landed: 64 static, 36 enabled, 10 alias,
    # 5 disabled, 3 masked, 1 indirect -- and `not-found` appeared in NONE of them,
    # so this condition cannot manufacture a false absence out of a real unit.
    # SCOPE, their caveat kept rather than dropped: that is 120 units on ONE box.
    # systemd's vocabulary is not box-specific so it should generalise, but it was
    # measured on one machine. Adjacent and NOT handled here: an aliased unit
    # reports `alias`, not its target's state, so a memory naming an alias is
    # compared against a different string than the operator meant.
    if enabled == "not-found":
        return [{"surface": "systemd-runtime", "mode": "observed",
                 "observed": "absent", "error": None}]

    return [{"surface": "systemd-runtime", "mode": "observed",
             "observed": {"active": active, "enabled": enabled},
             "error": None}]


def _listen_addr(c):
    # ss sees this box only (GC3c). Latency is not the discriminator here —
    # a local table walk is always cheap, and applying SCOPE_FLOOR_S would
    # turn every not-listening into not_my_scope.
    t0 = time.perf_counter()
    table = _run(["ss", "-lnt"])
    elapsed = time.perf_counter() - t0
    return [{"surface": "kernel-sockets", "mode": "observed",
             "observed": "listening" if c["ref"] in table else "not-listening",
             "elapsed": elapsed, "error": None}]


def _unit_bind(c):
    """TWO surfaces for one claim -- GC4. `ref` is the .service unit, produced by
    candidates._escalate_bindings; `asserted` is the address the memory names."""
    unit = _run(["systemctl", "cat", c["ref"]])
    t0 = time.perf_counter()
    sockets = _run(["ss", "-lnt"])
    elapsed = time.perf_counter() - t0
    out = [{"surface": "systemd-unit", "mode": "declared",
            "observed": c["asserted"] if c["asserted"] in unit else "not-declared",
            "error": None},
           {"surface": "kernel-sockets", "mode": "observed",
            "observed": c["asserted"] if c["asserted"] in sockets else "not-listening",
            "elapsed": elapsed, "error": None}]
    # A third surface only when the unit is actually a container unit. The
    # container name comes from the UNIT TEXT, never from memory prose.
    m = re.search(r"--name[= ]([\w.-]+)", unit)
    if m:
        try:
            info = json.loads(_run(["docker", "inspect", m.group(1)]))
            out.append({"surface": "docker-inspect", "mode": "observed",
                        "observed": json.dumps(info[0]["HostConfig"]["PortBindings"]),
                        "error": None})
        except Exception as e:
            out.append({"surface": "docker-inspect", "mode": "observed",
                        "observed": None, "error": repr(e)})
    return out


def _http_endpoint(c):
    """GET only. Status + Content-Type; body discarded. GC3d lives here.

    urllib, not curl: curl is not in _ALLOWED, and a denylist would let
    `curl -X POST` through. Elapsed is recorded on every exit. An absence
    cheaper than SCOPE_FLOOR_S is `not_my_scope` — about the instrument.
    """
    url = c["ref"]
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = str(resp.status)
            ctype = resp.headers.get("Content-Type", "")
            resp.read()
        elapsed = time.perf_counter() - t0
        return [{"surface": "http", "mode": "observed",
                 "observed": {"status": status, "content_type": ctype},
                 "elapsed": elapsed, "error": None}]
    except urllib.error.HTTPError as e:
        # >>> AN HTTPError IS A SERVER ANSWERING. <<< `except Exception` caught it
        # and wrote "refused", so a live endpoint returning 403/404/405/502 was
        # recorded as unreachable — a confidently WRONG observation, not noise.
        # MEASURED 2026-09-04 (a peer, seat-A review): 14 of the 16
        # "refused" endpoints on the real corpus actually answer. gbrain
        # http://10.0.0.1:8792/mcp -> 405, alive, counted refused TWICE.
        #
        # GC3a is already written into this plan — "the CLAIM decides what counts
        # as alive, not the response code; only connection refused or timeout is
        # absence" — and the implementation swallowed that distinction one layer
        # below the rule stating it.
        elapsed = time.perf_counter() - t0
        return [{"surface": "http", "mode": "observed",
                 "observed": {"status": str(e.code),
                              "content_type": (e.headers.get("Content-Type", "")
                                               if e.headers else "")},
                 "elapsed": elapsed, "error": None}]
    except Exception as e:
        elapsed = time.perf_counter() - t0
        if elapsed < SCOPE_FLOOR_S:
            return [{"surface": "http", "mode": "observed",
                     "observed": None, "elapsed": elapsed,
                     "error": "not_my_scope"}]
        if "Timeout" in type(e).__name__ or "timed out" in str(e).lower():
            return [{"surface": "http", "mode": "observed",
                     "observed": None, "elapsed": elapsed, "error": "timeout"}]
        return [{"surface": "http", "mode": "observed",
                 "observed": "refused", "elapsed": elapsed, "error": None}]


TABLE = {"path": _path, "pkg_version": _pkg_version, "unit_state": _unit_state,
         "listen_addr": _listen_addr, "unit_bind": _unit_bind,
         "http_endpoint": _http_endpoint}


def probe(candidate: dict) -> list[dict]:
    fn = TABLE.get(candidate.get("kind"))
    if fn is None:
        return []          # -> unprobeable verdict in verify.py, never a silent skip
    try:
        return fn(candidate)
    except Exception as e:
        return [{"surface": candidate["kind"], "observed": None, "error": repr(e)}]
