#!/usr/bin/env python3
"""Triage row backends (card #1073). The store (rows.py) talks to this
protocol; production rows live on board cards titled "<owner> triage".
"""
from __future__ import annotations


class BackendError(RuntimeError):
    """A named board failure: what was attempted, the HTTP status, and
    the gateway's own detail. Raised, never a row with id None."""


class MemoryBackend:
    """In-memory double for tests: rows are dicts, posts are dicts.

    Strict where the gateway is strict (card #1073, #1446): a create
    without a project_id is refused, and a post to a thread that does
    not exist is refused — the old permissive double is what let the
    0.72.14 live failure pass every test.
    """

    def __init__(self, project_id=9):
        self.rows: dict[str, dict] = {}
        self.posts: dict[str, list[dict]] = {}
        self.next_id = 1
        self.project_id = project_id

    def find_open_row(self, title):
        return self.rows.get(title)

    def create_row(self, title, owner):
        if self.project_id is None:
            raise BackendError(
                f"create '{title}': project_id is required — the gateway "
                f"refuses a card create without one (BoardCardCreate)")
        row = {"id": self.next_id, "title": title, "owner": owner,
               "project_id": self.project_id, "status": "open"}
        self.next_id += 1
        self.rows[title] = row
        self.posts[title] = []
        return row

    def list_posts(self, row):
        return list(self.posts.get(row["title"], []))

    def append(self, row, body, kind="item"):
        if row["title"] not in self.posts:
            raise BackendError(
                f"post to '{row['title']}': no such thread — the row was "
                f"never created")
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

    def __init__(self, gateway, token, sender, http_get, http_post,
                 project_id=9):
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.sender = sender
        self._http_get = http_get
        self._http_post = http_post
        self.project_id = project_id

    @staticmethod
    def _detail(body) -> str:
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
        return "<no detail>"

    def _refused(self, what: str, status, body) -> BackendError:
        return BackendError(
            f"{what}: gateway refused {status}: {self._detail(body)}")

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
        # BoardCardCreate REQUIRES project_id (card #1073, #1446: the
        # 0.72.14 live run POSTed title+body only, was refused, and the
        # old code carried the refusal forward as a row with id None).
        # The owner is the card assignee, so the row lands in a queue.
        payload = {"actor": self.sender, "project_id": self.project_id,
                   "title": title,
                   "body": f"Daily triage row for {owner} (card #1073). "
                           "Items are appended by the triage job; "
                           "owners work them off the row.",
                   "assignee": owner}
        try:
            status, body = self._http_post(
                f"{self.gateway}/board/cards", payload, self.token)
        except Exception as exc:
            raise BackendError(f"create '{title}': {exc}") from exc
        if status is None or not 200 <= status < 300:
            raise self._refused(f"create '{title}'", status, body)
        card = body if isinstance(body, dict) else {}
        if not isinstance(card.get("id"), int):
            raise BackendError(
                f"create '{title}': gateway replied {status} with no card "
                f"id: {self._detail(body)}")
        return {"id": card.get("id"), "title": title, "owner": owner,
                "status": "open"}

    def _thread_uuid(self, row):
        if not row.get("id"):
            raise BackendError(
                f"post to '{row.get('title')}': the row has no board id — "
                f"its create failed and was not reported")
        try:
            status, body = self._http_get(
                f"{self.gateway}/board/cards/{row['id']}", self.token)
        except Exception as exc:
            raise BackendError(
                f"thread of '{row.get('title')}': {exc}") from exc
        if status != 200 or not isinstance(body, dict):
            raise self._refused(f"thread of '{row.get('title')}'", status,
                                body)
        thread_uuid = body.get("thread_uuid")
        if not thread_uuid:
            raise BackendError(
                f"thread of '{row.get('title')}': card has no thread")
        return thread_uuid

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
        try:
            status, resp = self._http_post(
                f"{self.gateway}/messages",
                {"from_node": self.sender,
                 "to_node": row.get("owner") or self.sender,
                 "kind": "answer" if kind == "security" else "fyi",
                 "content": body, "thread_id": thread_uuid},
                self.token)
        except Exception as exc:
            raise BackendError(
                f"append to '{row.get('title')}': {exc}") from exc
        if status is None or not 200 <= status < 300:
            raise self._refused(f"append to '{row.get('title')}'", status,
                                resp)
        msg = resp if isinstance(resp, dict) else {}
        return {"id": msg.get("id"), "body": body, "kind": kind}
