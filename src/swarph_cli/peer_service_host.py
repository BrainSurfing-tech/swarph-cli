"""Bounded service host for one claimed peer spool job at a time.

This module deliberately has no gateway client, terminal integration, or queue
mutation. A platform-specific provider is the only authority that may obtain
the DM payload after the host has claimed its durable envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from swarph_cli.peer_executor import (
    PeerExecutorError,
    PeerService,
    envelope_digest,
    output_digest,
    source_ref_from_delivery_ref,
)


@dataclass(frozen=True)
class PeerPayloadRequest:
    job_id: str
    peer: str
    source_dm_id: int
    queue_claim_fence: int
    service_fencing_token: int


@dataclass(frozen=True)
class PeerPayload:
    peer: str
    source_dm_id: int
    queue_claim_fence: int
    text: str


class PeerPayloadProvider(Protocol):
    """The sole authority that resolves payload content for a claimed DM."""

    def get_payload(self, request: PeerPayloadRequest) -> PeerPayload: ...


class PeerActionHandler(Protocol):
    """A bounded action implementation that receives only one payload."""

    def handle(self, request: PeerPayloadRequest, payload: str) -> str: ...


class PeerServiceHost:
    """Claim, authorize, execute, and receipt one spool job without queue access."""

    def __init__(
        self,
        service: PeerService,
        provider: PeerPayloadProvider | None,
        handler: PeerActionHandler | None,
        *,
        max_payload_chars: int = 32_000,
        max_output_chars: int = 32_000,
    ):
        if provider is None:
            raise PeerExecutorError("peer payload provider is required")
        if handler is None:
            raise PeerExecutorError("peer action handler is required")
        if not isinstance(max_payload_chars, int) or max_payload_chars < 1:
            raise PeerExecutorError("max_payload_chars must be a positive integer")
        if not isinstance(max_output_chars, int) or max_output_chars < 1:
            raise PeerExecutorError("max_output_chars must be a positive integer")
        self.service = service
        self.provider = provider
        self.handler = handler
        self.max_payload_chars = max_payload_chars
        self.max_output_chars = max_output_chars

    def execute(self, job_id: str) -> dict:
        """Execute one pending job or fail without manufacturing completion."""
        claim = self.service.claim(job_id)
        job = self.service.claimed_job(job_id, claim["fencing_token"])
        source_ref = source_ref_from_delivery_ref(job)
        request = PeerPayloadRequest(
            job_id=job_id,
            peer=job["destination_peer"],
            source_dm_id=job["source_dm_id"],
            queue_claim_fence=source_ref["queue_claim_fence"],
            service_fencing_token=claim["fencing_token"],
        )
        payload = self.provider.get_payload(request)
        self._validate_payload(payload, request)
        result = self.handler.handle(request, payload.text)
        if not isinstance(result, str) or len(result) > self.max_output_chars:
            raise PeerExecutorError("handler returned an invalid or oversized output")
        return self.service.produce_receipt(
            job_id,
            claim["fencing_token"],
            result,
            source_ref,
            output_digest(payload.text),
            envelope_digest(job),
        )

    def _validate_payload(
        self, payload: PeerPayload, request: PeerPayloadRequest
    ) -> None:
        if not isinstance(payload, PeerPayload):
            raise PeerExecutorError("peer payload provider returned an invalid payload")
        if (
            payload.peer,
            payload.source_dm_id,
            payload.queue_claim_fence,
        ) != (
            request.peer,
            request.source_dm_id,
            request.queue_claim_fence,
        ):
            raise PeerExecutorError("peer payload does not match the claimed envelope")
        if (
            not isinstance(payload.text, str)
            or len(payload.text) > self.max_payload_chars
        ):
            raise PeerExecutorError("peer payload is invalid or oversized")
