"""Stream a synthetic no-regression corpus across two redact.py revisions.

Run: python3 tests/redact_1710_adversarial.py --base /path/to/base/src/swarph_cli/redact.py
"""

import argparse
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import sys


SRC = Path(__file__).resolve().parents[1] / "src"
MARKER = re.compile(r"\[REDACTED(?::[^\]]*)?\]")
JOIN = ("", "_", "-", ".", "/", ":", "@", "=", "&", ";", ",", " ", "é", "中", "🪵", "][")
LEAD = ("", "prefix", "123", "_", "prefix-", "prefix.", "é", "中", "🪵", "field=")
TAIL = ("", "suffix", "_", "-", ".invalid", "\"", ")", "é", "中", "🪵")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.redact_text


def plant(kind, tag, variant):
    lower = "sample" + tag + "only"
    upper = "SAMPLE" + tag + "ONLY"
    if kind == 0:
        return ("sk-", "sk-proj-", "sk-svcacct-")[variant % 3] + lower + "a" * 12
    if kind == 1:
        return ("sk-ant-", "sk-ant-api03-")[variant % 2] + lower
    if kind == 2:
        return ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")[variant % 6] + lower + "a" * 12
    if kind == 3:
        return "xai-" + lower + "a" * 12
    if kind == 4:
        return "AIza" + lower.ljust(35, "a")
    if kind == 5:
        return "xox" + "baprs"[variant % 5] + "-" + lower
    if kind == 6:
        return ("AKIA", "ASIA")[variant % 2] + upper[2:]
    if kind == 7:
        return "postgresql://sample:" + lower + "@host.invalid"
    if kind == 8:
        return "password='" + lower + "'"
    if kind == 9:
        return "Bearer " + lower + "a" * 12
    if kind == 10:
        return "eyJ" + lower + ".eyJsynthetic.signature"
    if kind == 11:
        return "-----BEGIN PRIVATE KEY-----" + lower + "-----END PRIVATE KEY-----"
    if kind == 12:
        return "aws_session_token=" + lower + "a" * 12
    if kind == 13:
        return "mysql -p" + lower
    if kind == 14:
        prefix = ("CUSTOM:7", "field7")[variant % 2]
        return "[REDACTED:" + prefix + "AKIA" + upper[2:] + "]"
    if kind == 15:
        return "app_auth=example_" + lower
    return "not-a-secret-" + lower


def corpus(seed, lines):
    rng = random.Random(seed)
    providers = tuple(range(7))
    for row in range(lines):
        kinds = rng.choices(range(17), k=3)
        mode = row % 8
        if mode == 0:
            kinds[:2] = (7, rng.choice(providers))
        elif mode == 1:
            kinds[:2] = rng.choices(providers, k=2)
        elif mode == 2:
            kinds[:2] = (rng.choice((8, 9, 10, 11, 12, 13)), rng.choice(providers))
        elif mode == 3:
            kinds[:2] = (rng.choice(providers), rng.randrange(7, 14))
        elif mode == 4:
            kinds[:] = (16, 15, 14)
        elif mode == 5:
            kinds[:2] = (14, rng.choice(providers))
        tags = tuple(f"{row * 3 + slot:08d}" for slot in range(3))
        variants = rng.getrandbits(24)
        a, b, c = (plant(kind, tag, variants >> (8 * slot)) for slot, (kind, tag) in enumerate(zip(kinds, tags)))
        join = "" if mode <= 3 else rng.choice(JOIN)
        text = rng.choice(LEAD) + a + join + b + rng.choice(JOIN) + c + rng.choice(TAIL)
        yield text, tags, kinds


def compare(before, after, tags):
    # Per-plant comparison prevents one newly redacted secret from canceling a leak.
    delta = tuple(after.count(tag) - before.count(tag) for tag in tags)
    new = iter(MARKER.findall(after))
    marker_loss = not all(any(expected == found for found in new) for expected in MARKER.findall(before))
    return delta, marker_loss


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=SRC / "swarph_cli/redact.py")
    parser.add_argument("--lines", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=1710)
    args = parser.parse_args()
    if not 0 < args.lines < 10**8 // 3:
        parser.error("--lines must be positive and fit the fixed-width tags")
    for key in ("SWARPH_REDACT_OPTOUT", "SWARPH_REDACT_CUSTOM_RULES"):
        if os.environ.get(key):
            parser.error(f"unset {key} before comparing revisions")
    sys.path.insert(0, str(SRC))
    base = load(args.base, "base_redact")
    candidate = load(args.candidate, "candidate_redact")
    assert compare("1111 [REDACTED:A]", "2222 [REDACTED:A]", ("1111", "2222")) == ((-1, 1), False)
    assert compare("[REDACTED:A] [REDACTED:B]", "[REDACTED:B] [REDACTED:A]", ()) == ((), True)

    stats = Counter(main_hidden=0, lost=0, gained=0, changed_lines=0, marker_loss_lines=0, clean_changed_lines=0)
    examples = []
    for row, (text, tags, kinds) in enumerate(corpus(args.seed, args.lines)):
        before, after = base(text), candidate(text)
        stats["main_hidden"] += sum(kind < 14 and tag not in before for kind, tag in zip(kinds, tags))
        if before == after:
            continue
        delta, marker_loss = compare(before, after, tags)
        clean_change = all(kind >= 14 for kind in kinds)
        stats["lost"] += sum(d > 0 for d in delta)
        stats["gained"] += sum(d < 0 and kind < 14 for d, kind in zip(delta, kinds))
        stats["changed_lines"] += 1
        stats["marker_loss_lines"] += marker_loss
        stats["clean_changed_lines"] += clean_change
        if (marker_loss or clean_change or any(d > 0 for d in delta)) and len(examples) < 5:
            examples.append(dict(row=row, input=text, base=before, candidate=after, tag_deltas=delta))
    print(json.dumps(dict(seed=args.seed, lines=args.lines, **stats, examples=examples)))
    return int(not stats["main_hidden"] or bool(examples))


if __name__ == "__main__":
    raise SystemExit(main())
