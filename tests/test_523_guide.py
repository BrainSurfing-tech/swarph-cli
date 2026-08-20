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

from swarph_cli.commands.guide import (_commands_in, _load_guide, _search,
                                        _split_topics, run_guide)


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
    """EXECUTION on the condition, with the claim CORRECTED to what it actually proves.

    >>> "PYTHONIOENCODING=cp1252 REPRODUCES A FRENCH WINDOWS CONSOLE EXACTLY" WAS TOO
    STRONG, AND gpu-wsl DISPROVED IT BY TRYING TO REPRODUCE THE CRASH AND FAILING. <<<
    On a genuine fr-FR box he set `chcp 1252` AND `PYTHONUTF8=0`, ran the PRE-fix
    commit, and got no crash: `stdout.encoding` stayed utf-8 because that box's system
    ANSI codepage (HKLM ... CodePage ACP) is 65001 -- the Windows "Use Unicode
    UTF-8 worldwide" beta setting. `chcp` moves the CONSOLE display codepage; it does
    not move the ACP that `locale.getpreferredencoding()` reads.

    So FRENCH LOCALE IS NOT A PROXY FOR A cp1252 ACP, and this env var models an
    8-bit-stdout console -- a real configuration many boxes have -- rather than "a
    French Windows box" specifically.

    Worth keeping honest for a second reason: lab never saw the original traceback. The
    commander reported `--search` "doesn't work" and the cp1252 mechanism was INFERRED
    from the --list/--search asymmetry. The inference is plausible and the fix is right
    either way, but the crash-to-fix transition on a real box is UNWITNESSED."""
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


# ── Windows-seat review of 0.45.0: gpu-wsl + cursor-win, independently ──────

def test_the_guide_gives_a_WINDOWS_supervision_answer():
    """>>> FOUND BY TWO CELLS INDEPENDENTLY (gpu-wsl and cursor-win), which is what
    made it undeniable. <<<

    start-here step 1 explicitly acknowledges Windows ("you also need a multiplexer:
    psmux"). Step 4 then handed every reader `sudo systemctl enable --now` with no
    alternative, and check-your-own-setup repeated the systemd-only check.

    cursor-win's phrasing is the reason this is a bug and not an omission: the silence
    reads as "they forgot me" PRECISELY BECAUSE step 1 acknowledged them. A guide that
    never mentioned Windows would be merely incomplete; one that mentions it and then
    stops is a route that dead-ends. That is the #520 defect -- naming a destination
    without a route -- inside the fix for #520."""
    text = _load_guide()
    topics = _split_topics(text)
    assert "schtasks" in topics["start-here"], "no Windows supervision instruction"
    assert "psmux" in topics["start-here"]
    assert "schtasks" in topics["check-your-own-setup"], (
        "the liveness table must be answerable on Windows too -- step 4 sends them here")

    # >>> THIS TEST PASSED ON A TWO-THIRDS FIX. <<< It named the two sections I had
    # thought of and stopped, so `how-to`'s supervision row kept telling every reader
    # `sudo systemctl enable --now` with no alternative. gpu-wsl found it by RUNNING
    # `swarph guide how-to` on Windows rather than reading the diff.
    #
    # So the assertion is now DERIVED, not enumerated: any line anywhere in the guide
    # that prescribes systemctl must carry a Windows answer or point at one. A future
    # section I have not thought of is covered by construction.
    # SECTION-scoped, not line-scoped: start-here's systemd command sits in its own
    # fenced block with the Windows alternative in the block below it, which is correct
    # prose and would fail a per-line rule. The property is "a reader of THIS SECTION
    # gets a Windows route", so the section is the unit. (First version of this
    # assertion was line-scoped and flagged that correct prose -- an over-strict check
    # pushes the document toward a worse shape to satisfy it, same as the How-to row
    # that demanded `swarph ` earlier today.)
    for name, body in topics.items():
        if "systemctl enable" not in body:
            continue
        assert ("schtasks" in body or "#start-here" in body), (
            f"{name} prescribes systemctl with no Windows route anywhere in the section")


def test_the_guide_routes_to_the_wake_hook():
    """cursor-win: his commander typed `swarph guide wake` within an hour of the
    upgrade, because the waker is a verb now and that is the word he knows. The guide
    had ZERO mentions of it -- 'wake' appeared only as channel wake_policy.

    'How do I get woken' is among the likeliest intents on a fresh install, and the
    guide's own framing is that a cell arrives with an intent rather than a topic
    name."""
    text = _load_guide()
    topics = _split_topics(text)
    assert "swarph install-wake-hook" in topics["how-to"]
    assert "**wake hook**" in topics["glossary"]
    # and it must be reachable by the words a caller would actually use
    for term in ("woken", "wake hook"):
        assert _search(topics, term), f"{term!r} finds nothing"


@pytest.mark.parametrize("argv,expect_rc", [
    (["--search", "wake"], 0),      # the flag spelling
    (["search", "wake"], 0),        # >>> the spelling an LLM types FIRST <<<
    (["find", "wake"], 0),
    (["apropos", "wake"], 0),
    # SUPERSEDED 2026-08-20: a bare intent word now ANSWERS (rc=0) instead of naming
    # the alternative. This row asserted rc=2 and was correct for the design it was
    # written against -- the commander then changed the design: "its not searching
    # it's looking for a topic, so topics are the default", i.e. a bare word is an
    # INTENT and search is the fallback rather than a flag to learn. Kept as a row with
    # the new expectation rather than deleted, so the change of contract is visible
    # here and not only in a commit message.
    (["wake"], 0),
])
def test_every_dialect_of_the_intent_word(argv, expect_rc, capsys):
    """cursor-win measured all three on a live box and only ONE reached the careful
    error:

        guide --search wake    ok
        guide wake             "no topic 'wake'"   <- names alternatives
        guide search wake      argparse: unrecognized arguments  <- a USAGE DUMP that
                                                                    never mentions --search

    argparse rejected the third before run_guide was entered, so the branch built for
    exactly this failure could not fire. His conclusion is the fix: an LLM cell types
    `guide search wake` long before it types `--search`, so the natural spelling must
    BE the correct spelling."""
    assert run_guide(argv) == expect_rc
    cap = capsys.readouterr()
    if expect_rc == 0 and argv == ["wake"]:
        # answered rather than routed -- the point of the supersession above
        assert "**wake hook**" in cap.out


def test_search_returns_the_line_that_ANSWERS_not_the_first_line_that_matches():
    """>>> THE FIRST MATCHING LINE IS OFTEN THE WORST ONE. <<< Found by the commander
    simply running `swarph guide --search channel` on his box:

        channels    Channels                                    <- the heading. tautological.
        glossary    predecessor and polls no channels at all.   <- THE MONITOR DEFINITION

    The glossary hit is the one that matters: searching 'channel' returned the tail of
    the **monitor** entry, because it happens to mention channels and sorts earlier in
    the file than the channel definition itself. A result that merely CONTAINS the word
    outranked the one that DEFINES it -- so the search was answering 'where does this
    string appear' while the caller asked 'what is this'.

    Ranked now: a `**term**` definition beats a how-to row beats a command beats prose,
    and the section heading is skipped because it restates the topic name."""
    topics = _split_topics(_load_guide())
    hits = dict(_search(topics, "channel"))

    assert hits["glossary"].startswith("**channel**"), (
        f"glossary must return the channel DEFINITION, got: {hits['glossary']!r}")
    assert hits["channels"] != "Channels", "a section heading answers nothing"
    assert "swarph channel" in hits["channels"]


def test_a_definition_outranks_a_mention_in_the_same_section():
    """NON-VACUITY for the ranking: the glossary contains BOTH a line mentioning the
    word and a line defining it, and the definition must win regardless of file order."""
    topics = _split_topics(_load_guide())
    for term in ("channel", "obligation", "wake hook", "fan-out"):
        hit = dict(_search(topics, term)).get("glossary")
        assert hit and hit.lower().startswith(f"**{term}**"), (
            f"searching {term!r} should surface its glossary definition, got {hit!r}")


def test_search_never_returns_an_empty_or_heading_only_line():
    """A result the caller cannot act on is worse than one fewer result."""
    topics = _split_topics(_load_guide())
    for term in ("channel", "monitor", "wake", "board", "brain", "guide"):
        for name, line in _search(topics, term):
            assert line.strip(), f"empty hit for {term!r} in {name}"
            assert not line.startswith("#"), f"heading returned for {term!r} in {name}"


def test_an_ignored_argument_is_REFUSED_not_swallowed(capsys):
    """>>> AN IGNORED FILTER RETURNS AN UNFILTERED SUPERSET THAT LOOKS FILTERED. <<<

    `swarph guide --list hook` ran the full index and DISCARDED 'hook' in silence, so
    the caller reads a complete list as though it were hook-scoped. Found by the
    commander typing it.

    The gateway refuses exactly this on GET /messages, in those words -- so this CLI
    was doing what its own server forbids. The rule was already written down in this
    codebase; it just was not applied here."""
    for flag in (["--list"], ["--search", "wake"]):
        rc = run_guide([*flag, "hook"])
        assert rc == 2, f"{flag} + topic must refuse, got rc={rc}"
        err = capsys.readouterr().err
        assert "silently ignored" in err
        # and it must name BOTH readings, since either could be what was meant
        assert "--search hook" in err and "guide hook" in err


def test_the_command_index_finds_commands_inside_TABLE_ROWS():
    """`how-to` listed NO commands and it is nothing but commands -- 18 markdown table
    rows, each `| task | \\`swarph ...\\` |`. A startswith('swarph ') test saw none of
    them, so the sections that under-reported were exactly the most command-dense ones:
    how-to, check-your-own-setup, glossary.

    A `--list` that shows an em-dash against the richest section teaches the reader
    that section is empty."""
    topics = _split_topics(_load_guide())
    for section in ("how-to", "check-your-own-setup", "glossary"):
        cmds = _commands_in(topics[section])
        assert cmds, f"{section} reports no commands but contains several"
    # the wake hook is only ever mentioned inside a table row and a glossary line
    assert any("install-wake-hook" in c for c in _commands_in(topics["glossary"]))


def test_there_is_a_HOOKS_section_and_it_installs_something():
    """>>> "There is no information on hook installation." -- the commander, after I
    had already 'fixed' the wake-hook gap with ONE how-to row and ONE glossary line.
    <<<

    A row that names a verb is not documentation of a subsystem. There are three
    similarly-named hook verbs doing different jobs, a harness argument with three
    valid values, a two-valued install product depending on where the wake can live,
    and a LOUD REFUSAL path on an unknown harness. None of that fit in a table cell.

    Naming a destination without a route -- for the third time in this file, in the
    file written to fix exactly that."""
    topics = _split_topics(_load_guide())
    assert "hooks" in topics, "no Hooks section"
    h = topics["hooks"]
    # the invocation the commander actually types, with both flags
    assert "swarph install-wake-hook --harness claude --cell <you>" in h
    # all three harnesses, because passing the wrong one is a hard refusal
    for harness in ("claude", "codex", "cursor"):
        assert harness in h, f"harness {harness!r} undocumented"
    # the refusal is the product: an unknown harness writes NOTHING and exits nonzero
    assert "REFUSAL" in h or "refusal" in h
    # and the verification step, since a hook in a config file is not a hook that works
    assert "swarph wake-hook-output" in h
    assert "--dry-run" in h


def test_the_three_hook_verbs_are_distinguished():
    """`install-wake-hook`, `install-hook` and `hooks` are three different things with
    near-identical names. A cell that installs the wrong one gets a working command, no
    error, and still no mail."""
    h = _split_topics(_load_guide())["hooks"]
    for verb in ("swarph install-wake-hook", "swarph install-hook", "swarph hooks"):
        assert verb in h, f"{verb} not distinguished from its siblings"


def test_the_hooks_section_is_reachable_by_the_word_a_caller_types():
    """The commander typed `swarph guide hook`. Singular, and not a topic name."""
    topics = _split_topics(_load_guide())
    assert run_guide(["hooks"]) == 0
    assert run_guide(["hook"]) == 0, "the singular must resolve via the substring fallback"
    assert any(name == "hooks" for name, _ in _search(topics, "deaf"))


def test_the_guide_does_not_recommend_a_BOX_GLOBAL_wake_hook():
    """>>> THE GUIDE RECOMMENDED THE DEFECT, FIFTEEN MINUTES AFTER I WROTE IT. <<<

    `install-wake-hook`'s default is `--scope user` -> ~/.claude/settings.json, which is
    BOX-GLOBAL. Combined with `--cell <you>` it instructs every claude cell on the machine
    to tail YOUR inbox, and the next install clobbers it.

    Measured on lab-ovh: six cells share that file and its hook was baked to one arbitrary
    cell (gpt-ops). Found by drop-on-meta-edge on his own box, where it named him.

    Two harms, and the second is the worse one: the neighbours are pointed at another
    cell's message stream, AND their own DMs go unwatched -- the exact failure this hook
    exists to prevent, inflicted on four cells by installing it for one."""
    h = _split_topics(_load_guide())["hooks"]
    assert "--scope project" in h, "the guide must recommend the per-cell scope"
    # Every RUNNABLE invocation must carry it. A line that merely NAMES the verb (the
    # three-verb disambiguation table) is not a command to copy -- narrowing to lines
    # carrying --harness, which is what an actual install looks like. The first version
    # of this assertion flagged that table row, which would have pushed flags into a
    # comparison cell to satisfy a test.
    invocations = [l for l in h.splitlines()
                   if "swarph install-wake-hook" in l and "--harness" in l]
    assert invocations, "the section shows no runnable install command"
    for line in invocations:
        assert "--scope project" in line, (
            f"an install command without --scope project recommends the box-global "
            f"default: {line.strip()!r}")
    assert "box-global" in h.lower(), "the hazard must be named, not just avoided"


# ── a bare word is an INTENT, not a topic name (commander, 2026-08-20) ──────

def test_a_bare_word_that_names_no_topic_SEARCHES_instead_of_failing(capsys):
    """>>> "its not searching its looking for a topic, so topics are the default" --
    the commander, after typing `swarph guide 'wake'` and getting a list of nouns. <<<

    The old path required the caller to already know our vocabulary, failed when they
    did not, and then TOLD THEM ABOUT A FLAG. That is a second round trip to reach an
    answer we already had -- the #520 defect (naming a destination without a route)
    surviving inside its own fix, one layer in: we routed them instead of answering.

    rc=0 now, not 2: an answer was produced. Failing is reserved for finding nothing."""
    rc = run_guide(["wake"])
    assert rc == 0, "a bare word with matches must ANSWER, not error"
    out = capsys.readouterr().out
    assert "No topic named 'wake'" in out, "still say the topic does not exist"
    assert "**wake hook**" in out, "the definition must be among the matches"


def test_the_topic_fallback_points_at_the_BEST_match_not_the_first(capsys):
    """The fallback prints `read one: swarph guide <x>`. That line is the actionable
    one, so it must name the best hit, not whichever section sorts earliest in the
    file. Before the sort, `guide wake` pointed at a how-to row about MUTING a channel
    -- the opposite of what the caller asked. AN ACTIONABLE LINE THAT NAMES THE WORST
    ANSWER IS WORSE THAN NO LINE."""
    run_guide(["wake"])
    out = capsys.readouterr().out
    body = out.split("Closest matches:", 1)[1]
    first_hit = body.strip().splitlines()[0].split()[0]
    assert f"read one:  swarph guide {first_hit}" in out
    assert first_hit == "glossary", (
        f"the wake-hook DEFINITION should rank first, got {first_hit!r}")


def test_an_exact_or_substring_topic_still_wins_over_search(capsys):
    """NON-VACUITY, and the property that keeps this change safe: `guide hooks` must
    print the SECTION, not search results, or the fallback has eaten the primary path."""
    assert run_guide(["hooks"]) == 0
    assert capsys.readouterr().out.startswith("## Hooks")
    assert run_guide(["hook"]) == 0          # substring
    assert capsys.readouterr().out.startswith("## Hooks")


def test_a_word_matching_NOTHING_still_refuses(capsys):
    """The refusal survives. Search-as-fallback must not turn every input into a
    success -- a guide that always answers cannot be trusted when it does."""
    assert run_guide(["zzzz-no-such-thing"]) == 2
    assert "Topics:" in capsys.readouterr().err
