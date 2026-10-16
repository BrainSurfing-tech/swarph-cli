"""launchd plists for the units a cell needs, rendered from the systemd units.

Card #1195. The plist is not a second copy of the command: each ProgramArguments
array is the unit's ExecStart, and a timer plist's StartInterval is that
timer's OnUnitActiveSec. tests/test_launchd_units.py fails if the two diverge.

launchd has no timer unit and does not expand systemd's %i, %h, or ${VAR}.
Those become __PEER__, __HOME__, and __VAR__ placeholders an install step
substitutes. The smoke substitutes them for one local peer and never for the
real mesh.

Ported, and why (lab note 73433: a cell's keep-alive set, not all 22 units):

- swarph-monitor@.service — the inbox monitor. Restart=always is KeepAlive.
  This is the process the macOS smoke kill -9s.
- swarph-watchdog.service — the stranded-session check. A oneshot: no
  KeepAlive, because a check that exits is not a crash.
- swarph-watchdog.timer — StartInterval from OnUnitActiveSec. The program is
  the service's ExecStart; launchd cannot point a timer at another plist.
- swarph-codex-outbox@.service — deliver queued Codex replies. The
  EnvironmentFile's ${VAR}s become placeholders. Not started by the smoke:
  a real run wants that cell's outbox paths.
- swarph-codex-outbox@.timer — StartInterval 15s, same program.
"""

from __future__ import annotations

import shlex
from pathlib import Path

_SYSTEMD = Path(__file__).resolve().parent / "systemd"
_LAUNCHD = Path(__file__).resolve().parent / "launchd"

# (plist file, label, service unit, timer unit or "", keepalive)
# Label keeps __PEER__ for an @-instance. A non-instance unit has no peer.
PORTED: tuple[tuple[str, str, str, str, bool], ...] = (
    (
        "swarph-monitor.plist",
        "tech.brainsurfing.swarph.monitor.__PEER__",
        "swarph-monitor@.service",
        "",
        True,
    ),
    (
        "swarph-watchdog.plist",
        "tech.brainsurfing.swarph.watchdog",
        "swarph-watchdog.service",
        "",
        False,
    ),
    (
        "swarph-watchdog.timer.plist",
        "tech.brainsurfing.swarph.watchdog.timer",
        "swarph-watchdog.service",
        "swarph-watchdog.timer",
        False,
    ),
    (
        "swarph-codex-outbox.plist",
        "tech.brainsurfing.swarph.codex-outbox.__PEER__",
        "swarph-codex-outbox@.service",
        "",
        False,
    ),
    (
        "swarph-codex-outbox.timer.plist",
        "tech.brainsurfing.swarph.codex-outbox.timer.__PEER__",
        "swarph-codex-outbox@.service",
        "swarph-codex-outbox@.timer",
        False,
    ),
)


def _seconds(spec: str) -> int:
    text = spec.strip()
    if text.endswith("min"):
        return int(text[:-3]) * 60
    if text.endswith("ms"):
        raise ValueError(f"refusing sub-second timer {spec!r}")
    if text.endswith("s"):
        return int(text[:-1])
    return int(text)


def _normalize(token: str) -> str:
    """One placeholder spelling for the systemd forms launchd will not expand."""
    if token == "<SWARPH_BIN>" or token == "/usr/local/bin/swarph":
        return "__SWARPH_BIN__"
    if token == "%i":
        return "__PEER__"
    if token == "%h":
        return "__HOME__"
    if token.startswith("<") and token.endswith(">") and token[1:-1].isupper():
        return f"__{token[1:-1]}__"
    if token.startswith("${") and token.endswith("}"):
        return f"__{token[2:-1]}__"
    return token


def _field(text: str, key: str) -> str | None:
    prefix = key + "="
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def program_arguments(service_text: str) -> list[str]:
    execstart = _field(service_text, "ExecStart")
    if not execstart:
        raise ValueError("unit has no ExecStart")
    return [_normalize(part) for part in shlex.split(execstart)]


def environment(service_text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in service_text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        item = line.split("=", 1)[1]
        key, _, value = item.partition("=")
        out.append((key, _normalize(value)))
    return out


def start_interval(timer_text: str) -> int:
    spec = _field(timer_text, "OnUnitActiveSec")
    if not spec:
        raise ValueError("timer has no OnUnitActiveSec")
    return _seconds(spec)


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _plist(label: str, argv: list[str], env: list[tuple[str, str]], *,
           keepalive: bool, interval: int | None, source: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        f"<!-- card #1195. Rendered from {source} by launchd_render.py. "
        "Do not edit by hand. -->",
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{_xml_escape(label)}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
    ]
    for arg in argv:
        lines.append(f"    <string>{_xml_escape(arg)}</string>")
    lines += ["  </array>", "  <key>RunAtLoad</key>"]
    # A bare oneshot is loaded but not fired. The timer plist is what fires it.
    # The monitor and the timer plists start when loaded.
    run = keepalive or interval is not None
    lines.append("  <true/>" if run else "  <false/>")
    if keepalive:
        lines += [
            "  <key>KeepAlive</key>",
            "  <true/>",
            "  <key>ThrottleInterval</key>",
            "  <integer>10</integer>",
        ]
    if interval is not None:
        lines += [
            "  <key>StartInterval</key>",
            f"  <integer>{interval}</integer>",
        ]
    if env:
        lines += ["  <key>EnvironmentVariables</key>", "  <dict>"]
        for key, value in env:
            lines.append(f"    <key>{_xml_escape(key)}</key>")
            lines.append(f"    <string>{_xml_escape(value)}</string>")
        lines.append("  </dict>")
    lines += [
        "  <key>StandardOutPath</key>",
        "  <string>__LOG_DIR__/launchd.out.log</string>",
        "  <key>StandardErrorPath</key>",
        "  <string>__LOG_DIR__/launchd.err.log</string>",
        "</dict>",
        "</plist>",
        "",
    ]
    return "\n".join(lines)


def render_template(plist_name: str) -> str:
    row = next(item for item in PORTED if item[0] == plist_name)
    _, label, service_name, timer_name, keepalive = row
    service = (_SYSTEMD / service_name).read_text(encoding="utf-8")
    argv = program_arguments(service)
    env = environment(service)
    interval = None
    source = f"systemd/{service_name}"
    if timer_name:
        timer = (_SYSTEMD / timer_name).read_text(encoding="utf-8")
        interval = start_interval(timer)
        source += f" + systemd/{timer_name}"
    return _plist(label, argv, env, keepalive=keepalive, interval=interval,
                  source=source)


def render_all() -> dict[str, str]:
    return {name: render_template(name) for name, *_ in PORTED}


def substitute(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        token = key if key.startswith("__") else f"__{key}__"
        if token not in out:
            raise KeyError(f"{token} is not in this plist")
        out = out.replace(token, value)
    leftover = [tok for tok in ("__SWARPH_BIN__", "__PEER__", "__HOME__",
                                "__GATEWAY__", "__LOG_DIR__") if tok in out]
    if leftover:
        raise ValueError(f"plist still has placeholders: {', '.join(leftover)}")
    return out


def write_templates(directory: Path | None = None) -> None:
    target = directory or _LAUNCHD
    target.mkdir(parents=True, exist_ok=True)
    for name, text in render_all().items():
        (target / name).write_text(text, encoding="utf-8")
