#!/usr/bin/env python3
"""Triage row backends (card #1073). The store (rows.py) talks to this
protocol; production rows live on board cards titled "<owner> triage".
"""
from __future__ import annotations


class MemoryBackend:
    """In-memory double for tests: rows are dicts, posts are dicts."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.posts: dict[str, list[dict]] = {}
        self.next_id = 1

    def find_open_row(self, title):
        return self.rows.get(title)

    def create_row(self, title, owner):
        row = {"id": self.next_id, "title": title, "owner": owner,
               "status": "open"}
        self.next_id += 1
        self.rows[title] = row
        self.posts[title] = []
        return row

    def list_posts(self, row):
        return list(self.posts.get(row["title"], []))

    def append(self, row, body, kind="item"):
        post = {"id": self.next_id, "body": body, "kind": kind}
        self.next_id += 1
        self.posts[row["title"]].append(post)
        return post


def _owner_of_title(title: str):
    suffix = " triage"
    if title.endswith(suffix):
        return title[: -len(suffix)]
    return None


class BoardBackend:
    """Live board backend: one card per owner titled "<owner> triage".

    Never closes, edits, or deletes anything (ruling 6): creates the card
    when missing and appends say-posts to its thread. All reads tolerate
    gateway errors by returning None/[] (unevaluable, never invented).
    """

    def __init__(self, gateway, token, sender, http_get, http_post):
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.sender = sender
        self._http_get = http_get
        self._http_post = http_post

    def find_open_row(self, title):
        try:
            status, body = self._http_get(f"{self.gateway}/board/cards",
                                          self.token)
        except Exception:
            return None
        if status != 200 or not isinstance(body, dict):
            return None
        cards = body.get("cards")
        if not isinstance(cards, list):
            return None
        for card in cards:
            if (isinstance(card, dict) and card.get("title") == title
                    and card.get("stage") != "closed"):
                return {"id": card.get("id"), "title": title,
                        "owner": _owner_of_title(title),
                        "status": "open"}
        return None

    def create_row(self, title, owner):
        try:
            status, body = self._http_post(
                f"{self.gateway}/board/cards",
                {"title": title,
                 "body": f"Daily triage row for {owner} (card #1073). "
                         "Items are appended by the triage job; "
                         "owners work them off the row."},
                self.token)
        except Exception:
            return {"id": None, "title": title, "status": "unknown"}
        card = body if isinstance(body, dict) else {}
        return {"id": card.get("id"), "title": title, "owner": owner,
                "status": "open"}

    def _thread_uuid(self, row):
        if not row.get("id"):
            return None
        try:
            status, body = self._http_get(
                f"{self.gateway}/board/cards/{row['id']}", self.token)
        except Exception:
            return None
        if status != 200 or not isinstance(body, dict):
            return None
        return body.get("thread_uuid")

    def list_posts(self, row):
        if not row.get("id"):
            return []
        try:
            status, body = self._http_get(
                f"{self.gateway}/board/cards/{row['id']}/thread", self.token)
        except Exception:
            return []
        if status != 200 or not isinstance(body, dict):
            return []
        msgs = body.get("messages")
        if not isinstance(msgs, list):
            return []
        return [{"id": m.get("id"), "body": m.get("content"),
                 "kind": m.get("kind")}
                for m in msgs if isinstance(m, dict)]

    def append(self, row, body, kind="item"):
        thread_uuid = self._thread_uuid(row)
        if thread_uuid is None:
            return {"id": None, "body": body, "kind": kind}
        try:
            status, resp = self._http_post(
                f"{self.gateway}/messages",
                {"from_node": self.sender,
                 "to_node": row.get("owner") or self.sender,
                 "kind": "answer" if kind == "security" else "fyi",
                 "content": body, "thread_id": thread_uuid},
                self.token)
        except Exception:
            return {"id": None, "body": body, "kind": kind}
        msg = resp if isinstance(resp, dict) else {}
        return {"id": msg.get("id"), "body": body, "kind": kind}
