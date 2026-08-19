"""Card #523 — `swarph guide` reads a bundled file and touches the network never.

The guide's audience is cells that have not set up yet, so every dependency it carries is
a cell it cannot reach. A gateway-served guide is unreachable before you are on the tailnet
(the mesh-gateway binds the tailnet IP only) and getting onto the tailnet is step one of
onboarding — so it could not serve the moment it exists for.

THE TEST THAT MATTERS IS THE PACKAGING ONE. `swarph guide` loads GUIDE.md via
importlib.resources. If it is not declared in [tool.setuptools.package-data] it is absent
from the wheel — and every test that reads it from the source tree still passes. That is
not hypothetical: 0.39.3 shipped a README telling peers to run scripts/ensure_monitor.sh
while the wheel did not contain the script, and a clean-room install is what caught it.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from swarph_cli.commands.guide import _load_guide, _split_topics, run_guide


def test_the_guide_loads_as_a_packaged_resource():
    """Loads via importlib.resources, which is what an installed wheel exercises.
    Reading via __file__ would work in the tree and break for everyone who pip-installs."""
    text = _load_guide()
    assert text.strip(), "GUIDE.md is empty"
    assert "swarph" in text.lower()


def test_guide_md_is_declared_as_package_data():
    """>>> THE 0.39.3 REGRESSION LOCK. <<< A guide absent from the wheel fails ONLY on a
    real install, where nobody is running the suite. Assert the declaration itself so the
    packaging mistake is caught in CI rather than by a peer whose install is broken."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    body = pyproject.read_text(encoding="utf-8")
    assert "guide/*.md" in body, (
        "GUIDE.md must be declared in [tool.setuptools.package-data] or it ships in no "
        "wheel — see the 0.39.3 note in pyproject.toml")


def test_every_table_of_contents_anchor_resolves():
    """The in-file table of contents links `#channels` etc. A link to a section that does
    not exist is the same defect as a published verb calling a missing endpoint (#496):
    the surface promises something it cannot deliver, on the page a newcomer reads first."""
    text = _load_guide()
    topics = _split_topics(text)
    linked = set()
    for line in text.splitlines():
        if line.startswith("| [") and "](#" in line:
            linked.add(line.split("](#", 1)[1].split(")", 1)[0])
    assert linked, "the table of contents has no anchors — did its format change?"
    missing = sorted(a for a in linked if a not in topics)
    assert not missing, f"table of contents links non-existent sections: {missing}"


def test_a_topic_returns_only_that_topic():
    topics = _split_topics(_load_guide())
    assert topics["channels"].startswith("## Channels")
    assert "## Start here" not in topics["channels"]
    assert "## The board" not in topics["channels"]


def test_an_unknown_topic_exits_nonzero_and_names_the_alternatives(capsys):
    """A refusal must say what to do instead. 'unknown topic' alone is the primer's own
    failure — naming a destination without a route."""
    rc = run_guide(["nonsense-topic"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Topics:" in err and "channels" in err


def test_a_substring_resolves_to_the_single_match(capsys):
    """`swarph guide channel` should find `channels` rather than refuse on a plural."""
    rc = run_guide(["channel"])
    assert rc == 0
    assert "## Channels" in capsys.readouterr().out


def test_the_guide_does_not_tell_cells_to_write_their_own_poller():
    """>>> THE SENTENCE THAT CAUSED CARD #520. <<< The previous onboarding document said
    'a 60-second poll loop ... ~80 lines of Python' and cells complied — producing bespoke
    pollers that read the wrong env var, ran unsupervised, and polled no channels at all.
    The guide must name `swarph monitor`, and this test exists so a future edit cannot
    quietly reintroduce the advice."""
    text = _load_guide()
    assert "swarph monitor start" in text, "the guide must give the supported invocation"
    lowered = text.lower()
    for banned in ("write your own poller", "~80 lines", "60-second poll loop that fetches"):
        # the guide MAY quote the old advice while warning against it; what it must not do
        # is present it as an instruction. Check it never appears outside a warning block.
        start = 0
        while (idx := lowered.find(banned, start)) != -1:
            # The window must SPAN the match, not stop before it: the warning's own
            # opening words are "Do not write your own poller", so a look-behind that
            # ends at the match can never contain them. (This test failed on its first
            # run for exactly that reason — the guide was correct, the check was not.)
            window = lowered[max(0, idx - 400):idx + len(banned) + 400]
            assert "do not write your own poller" in window, (
                f"{banned!r} at offset {idx} appears without its warning — the guide "
                "must never present a hand-rolled poller as an instruction (card #520)")
            start = idx + len(banned)


def test_no_network_call_in_the_guide_module():
    """Static guard on the property the whole card rests on. A future edit that adds a
    'fetch the latest guide' fallback would restore the gateway dependency and nothing
    else would notice until a newcomer could not onboard."""
    src = Path(__file__).resolve().parents[1] / "src/swarph_cli/commands/guide.py"
    body = src.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith("#")
    )
    for banned in ("urllib", "requests", "httpx", "socket", "urlopen"):
        assert banned not in code, (
            f"guide.py must not reference {banned!r} — the guide is offline BY DESIGN; "
            "a newcomer cannot reach the gateway before it is on the tailnet")


@pytest.mark.skipif(sys.platform == "win32", reason="path quoting differs on Windows")
def test_the_module_self_check_passes():
    """Execution, not import. `python -m swarph_cli.commands.guide` runs demo()."""
    r = subprocess.run(
        [sys.executable, "-m", "swarph_cli.commands.guide"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**__import__("os").environ,
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert r.returncode == 0, r.stderr
    assert "ok - " in r.stdout


# ── `apropos`: search by INTENT, not by knowing the topic's name ─────────────

def test_search_finds_a_topic_by_intent_not_by_its_name(capsys):
    """>>> THE DISCOVERY AFFORDANCE. <<< A cell arrives wanting to 'subscribe to
    updates'; it does not know the word 'channels'. Requiring the topic name is the
    #520 discovery defect reproduced inside its own fix — which is exactly what
    FreeDOS Help's `apropos` existed to solve."""
    rc = run_guide(["--search", "subscribe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "channels" in out
    assert "subscri" in out.lower()


def test_search_finds_the_board_by_the_word_a_caller_would_use(capsys):
    rc = run_guide(["--search", "owes"])
    assert rc == 0
    assert "the-board" in capsys.readouterr().out


def test_search_with_no_match_exits_nonzero_and_names_the_topics(capsys):
    rc = run_guide(["--search", "zzzz-no-such-thing"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Topics:" in err and "channels" in err


def test_the_list_is_an_index_of_COMMANDS_not_chapter_titles(capsys):
    """FreeDOS Help's contents screen lists commands you can type, so 'what can I do'
    and 'how do I do it' are one lookup. Each topic shows the commands it teaches."""
    rc = run_guide(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "swarph channel join" in out
    assert "swarph monitor start" in out
    assert "swarph mesh reply" in out


def test_the_command_index_does_not_repeat_an_entry(capsys):
    """Deduping the full line and truncating afterwards printed 'swarph board cards'
    three times — the dedupe has to run on the value actually printed."""
    run_guide(["--list"])
    for line in capsys.readouterr().out.splitlines():
        if "  " not in line:
            continue
        cmds = [c.strip() for c in line.split("  ", 1)[1].split(",") if c.strip()]
        assert len(cmds) == len(set(cmds)), f"repeated entry in: {line}"


# ── the WinHelp layer: tasks and a glossary ─────────────────────────────────

def test_the_how_to_section_is_phrased_as_tasks_not_topics():
    """WinHelp 3.1's 'How To...' lists VERBS ('Change an Icon'), not nouns ('Icons').
    A cell arrives with a task. A topic index makes it translate its goal into our
    vocabulary first — which is the step that fails."""
    topics = _split_topics(_load_guide())
    how = topics["how-to"]
    for task in ("subscribe to the weekly newsletter",
                 "answer something I was asked",
                 "publish my own feed for others to follow"):
        assert task in how, f"missing task phrasing: {task!r}"
    # every task row must carry a runnable command or an explicit pointer
    rows = [l for l in how.splitlines() if l.startswith("| ") and "---" not in l]
    for row in rows[2:]:  # skip header + separator
        # ANY runnable command, not only a swarph one — `sudo systemctl enable
        # --now swarph-monitor@<you>` is the correct answer to "stop losing my
        # messages when the monitor dies", and an assertion that demanded `swarph `
        # would have pushed the guide toward a worse answer to satisfy a test.
        assert "`" in row or "](#" in row, (
            f"a How-to row must give a command or a link, not just prose: {row}")


def test_the_glossary_defines_the_words_the_guide_itself_uses():
    """Jargon a cell meets in a DM before it meets a document. If the guide uses a
    term as though it were known, the glossary must define it — otherwise the guide
    has the same problem as the mesh it explains."""
    text = _load_guide()
    glossary = _split_topics(text)["glossary"]
    for term in ("wake_policy", "obligation", "fan-out", "monitor", "thread",
                 "codegraph", "membrane"):
        assert f"**{term}**" in glossary, f"{term!r} is used but never defined"


def test_search_reaches_the_how_to_and_glossary_sections(capsys):
    """The three layers must compose: a search by intent should surface the TASK,
    not only the prose section that happens to contain the word."""
    run_guide(["--search", "newsletter"])
    out = capsys.readouterr().out
    assert "how-to" in out, "a task-phrased row must be findable by search"


# ── the Windows report: `--search` died on a cp1252 console (card #527) ─────

def test_the_guide_is_pure_ascii():
    """>>> A DOCUMENT THAT MUST BE READABLE ON EVERY PLATFORM SHOULD NOT NEED A GUARD
    TO BE READABLE. <<< print_safe stops the crash and costs a mangled glyph; on a
    257-line onboarding page aimed at a cell that has just arrived, a page full of
    `?` is a poor first contact. 28 characters (em-dash x26, ellipsis, arrow) bought
    nothing that `--`, `...` and `->` do not."""
    text = _load_guide()
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, f"non-ASCII in GUIDE.md: {[hex(ord(c)) for c in bad]}"


def test_guide_py_never_uses_a_bare_print():
    """THE ACTUAL BUG, locked. `--list` worked on Windows and `--search` did not: the
    code path was identical, only the DATA differed. --list emits topic anchors and
    `swarph ...` commands (ASCII); --search emits matched PROSE. So a Linux test of
    both said nothing about either, and the failure was invisible to every check that
    ran before release.

    console_safe.print_safe already had 44 call sites when this module was written
    with bare `print`. Its own docstring says why: 'A protection sited inside one
    module protects one module.'"""
    src = (Path(__file__).resolve().parents[1]
           / "src/swarph_cli/commands/guide.py").read_text(encoding="utf-8")
    offenders = [ln.strip() for ln in src.splitlines()
                 if ln.lstrip().startswith("print(")]
    assert not offenders, (
        f"guide.py must print via console_safe.print_safe, found: {offenders}")


@pytest.mark.parametrize("argv", [["--list"], ["--search", "subscribe"],
                                  ["channels"], ["no-such-topic"]])
def test_every_path_survives_a_cp1252_console(argv):
    """EXECUTION, on the condition itself. PYTHONIOENCODING=cp1252 reproduces a French
    Windows console exactly, on Linux, in CI -- the environment modelled as data
    rather than as a platform we do not have."""
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys;from swarph_cli.commands.guide import run_guide;"
         "sys.exit(run_guide(sys.argv[1:]))", *argv],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**__import__("os").environ, "PYTHONIOENCODING": "cp1252",
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert "UnicodeEncodeError" not in r.stderr, r.stderr
    assert r.returncode in (0, 2), f"rc={r.returncode}\n{r.stderr}"
