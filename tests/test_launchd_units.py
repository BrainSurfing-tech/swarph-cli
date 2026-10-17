"""Card #1195: launchd plists are the systemd units, not a second command.

A hand-edited plist that no longer matches ExecStart fails here, on any OS.
The macOS runner is what proves launchd actually keeps the process up.
"""

from __future__ import annotations

from pathlib import Path

from swarph_cli.launchd_render import (
    PORTED,
    program_arguments,
    render_all,
    start_interval,
)

_LAUNCHD = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "launchd"
_SYSTEMD = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "systemd"


def test_shipped_plists_match_the_renderer():
    rendered = render_all()
    shipped = sorted(p.name for p in _LAUNCHD.glob("*.plist"))
    assert shipped == sorted(rendered), (
        f"launchd/ has {shipped}, the renderer emits {sorted(rendered)}"
    )
    for name, text in rendered.items():
        assert (_LAUNCHD / name).read_text(encoding="utf-8") == text, (
            f"{name} drifted from its systemd unit; regenerate with "
            f"launchd_render.write_templates()"
        )


def test_monitor_command_is_the_unit_execstart():
    unit = (_SYSTEMD / "swarph-monitor@.service").read_text(encoding="utf-8")
    assert program_arguments(unit) == [
        "__SWARPH_BIN__", "monitor", "start", "--as", "__PEER__",
        "--deliver", "pull", "--foreground",
    ]
    plist = (_LAUNCHD / "swarph-monitor.plist").read_text(encoding="utf-8")
    assert "<key>KeepAlive</key>" in plist
    assert "<key>ThrottleInterval</key>\n  <integer>10</integer>" in plist
    assert "__GATEWAY__" in plist
    assert "Restart=always" in unit


def test_timer_intervals_come_from_the_systemd_timers():
    watchdog = (_SYSTEMD / "swarph-watchdog.timer").read_text(encoding="utf-8")
    outbox = (_SYSTEMD / "swarph-codex-outbox@.timer").read_text(encoding="utf-8")
    assert start_interval(watchdog) == 300
    assert start_interval(outbox) == 15
    assert "<integer>300</integer>" in (
        _LAUNCHD / "swarph-watchdog.timer.plist"
    ).read_text(encoding="utf-8")
    assert "<integer>15</integer>" in (
        _LAUNCHD / "swarph-codex-outbox.timer.plist"
    ).read_text(encoding="utf-8")


def test_ported_set_is_the_keep_alive_units_only():
    services = {row[2] for row in PORTED}
    assert services == {
        "swarph-monitor@.service",
        "swarph-watchdog.service",
        "swarph-codex-outbox@.service",
    }
    # A oneshot is not KeepAlive. Only the monitor restarts after kill -9.
    keepalive = {row[0] for row in PORTED if row[4]}
    assert keepalive == {"swarph-monitor.plist"}
