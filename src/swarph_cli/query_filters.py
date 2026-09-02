"""Fail-CLOSED query-filter allowlists (#356).

An unrecognized filter parameter on a list endpoint fails OPEN in FastAPI
(ignored) and returns a SUPERSET that reads as a working filtered result.
Specimen: ``unread=1`` on GET /messages is dropped; ``unread_only=true``
filters. The wrong name returned 50 rows while the DB had 0 unread.

UNKNOWN MUST NOT BE TREATED AS "NO FILTER". Refuse before the request, and
400 on the server if a raw client still sends the name.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# GET /messages — names FastAPI actually binds (incl. documented aliases).
MESSAGES_GET_PARAMS = frozenset({
    "to",
    "to_node",
    "from",
    "from_node",
    "channel",
    "thread_id",
    "kind",
    "unread_only",
    "since",
    "limit",
})

# path suffix → allowlist. Only GET query endpoints that FILTER.
ENDPOINT_QUERY_ALLOWLISTS = (
    ("/messages", MESSAGES_GET_PARAMS),
)


class UnknownQueryFilter(ValueError):
    """A query key is not in the endpoint's accepted set."""


def unknown_query_params(provided, allowed) -> frozenset[str]:
    """Keys in ``provided`` that ``allowed`` does not name."""
    return frozenset(provided) - frozenset(allowed)


def refuse_unknown_query_params(provided, allowed, *, endpoint: str) -> None:
    extra = sorted(unknown_query_params(provided, allowed))
    if extra:
        raise UnknownQueryFilter(
            f"unrecognized query filter(s) {extra} on {endpoint}; "
            f"accepted: {sorted(allowed)}. Refusing rather than ignoring — "
            f"an unknown filter would return an unfiltered superset."
        )


def allowlist_for_url(url: str) -> "frozenset[str] | None":
    """Allowlist for a GET URL, or None if this pack does not cover the path."""
    path = urlparse(url).path.rstrip("/") or "/"
    for suffix, allowed in ENDPOINT_QUERY_ALLOWLISTS:
        if path == suffix or path.endswith(suffix):
            return allowed
    return None


def refuse_unknown_query_on_url(url: str) -> None:
    """Raise if the URL carries a query key this endpoint does not accept."""
    allowed = allowlist_for_url(url)
    if allowed is None:
        return
    keys = parse_qs(urlparse(url).query, keep_blank_values=True).keys()
    refuse_unknown_query_params(keys, allowed, endpoint=urlparse(url).path)
