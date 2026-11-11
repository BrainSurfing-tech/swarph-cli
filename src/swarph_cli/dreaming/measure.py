"""Offline recall instrument. Questions/answers are pilot inputs, never generated here."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from swarph_cli.dreaming import store
from swarph_cli.dreaming.store import Refused


def questions(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    rows = data.get("questions", [])
    if not isinstance(data.get("tag"), str) or not data["tag"] or len(rows) < 30:
        raise Refused("recall needs a tagged frozen set of at least 30 questions")
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or row["id"] in ids or not isinstance(row.get("question"), str)
                or type(row.get("answer")) not in (str, int)):
            raise Refused("recall requires unique ids and script-matchable string/int answers")
        ids.add(row["id"])
    return data, store.digest(raw)


def pin(state, path):
    receipt = state / "first-propose.json"
    if receipt.exists():
        pinned = json.loads(receipt.read_text())
        if path and questions(path)[1] != pinned["question_sha256"]:
            raise Refused("question set cannot change after first propose")
        return
    data, sha = questions(path) if path else ({}, None)
    recorded = time.time_ns()
    store.atomic_write(receipt, json.dumps({"question_sha256": sha,
        "question_set_tag": data.get("tag"), "recorded_at": recorded,
        "first_pass_at": time.time_ns()}).encode())


def score(args, state):
    data, sha = questions(args.questions)
    pinned = json.loads((state / "first-propose.json").read_text())
    if (sha != pinned["question_sha256"] or data["tag"] != pinned["question_set_tag"]
            or pinned["recorded_at"] > pinned["first_pass_at"]):
        raise Refused("questions were not frozen before first propose")
    before = json.loads(Path(args.before).read_text())
    after = json.loads(Path(args.after).read_text())
    if (not isinstance(before.get("model"), str) or not before["model"]
            or before["model"] != after.get("model")):
        raise Refused("before/after reader model tags must match")
    expected = {row["id"]: str(row["answer"]).strip() for row in data["questions"]}
    values = []
    for answers in (before, after):
        if (answers.get("question_set_sha256") != sha or answers.get("question_set_tag") != data["tag"]
                or set(answers.get("answers", {})) != set(expected)):
            raise Refused("answer ids or frozen question tags do not match")
        if any(type(v) not in (str, int) for v in answers["answers"].values()):
            raise Refused("answers must be script-matchable strings/ints")
        values.append(sum(str(answers["answers"][key]).strip() == answer for key, answer in expected.items()))
    return {"questions": len(expected), "before_correct": values[0],
            "after_correct": values[1], "paired_difference": values[1] - values[0]}


def counts(notes):
    indexes = {n: t for n, t in notes.items() if n.startswith(("MEMORY", "index_")) or n == "_index.md"}
    targets = []
    for text in indexes.values():
        for markdown, wiki in re.findall(r"\]\(([\w.-]+\.md)\)|\[\[([\w.-]+)\]\]", text):
            name = markdown or wiki
            targets.append(name if name.endswith(".md") else name + ".md")
    return {"files": len(notes), "index_bytes": sum(len(t.encode()) for t in indexes.values()),
            "feedback_files": sum(n.startswith("feedback_") for n in notes),
            "unresolved_index_links": sum(n not in notes for n in targets)}
