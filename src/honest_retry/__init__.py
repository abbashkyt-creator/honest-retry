"""Retry HTTP requests only when it is safe, and say honestly what happened.

A failed request is not always a request that did not happen. If the
connection was refused, nothing was sent and a retry is safe. If the connection
dropped after the request went out, the server may already have acted on it,
and retrying a POST can do the thing twice: charge twice, order twice, bid
twice.

``send`` classifies every attempt and retries only what is safe for the
method:

* ``not_sent``: nothing reached the server (refused, DNS, no free connection,
  429). Always safe to retry.
* ``unconfirmed``: it may have been received (reset, read timeout, 5xx). Retried
  only for idempotent methods, or when an Idempotency-Key lets the server
  de-duplicate.
* ``done``: the server answered with a final status. The caller acts on it.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

__all__ = ["send", "Result", "IDEMPOTENT_METHODS"]
__version__ = "0.1.0"

Outcome = Literal["done", "not_sent", "unconfirmed"]

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})

# Raised before any byte of the request reached the server.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Raised after the request may have been (partly) delivered.
_MAYBE_SENT_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


@dataclass
class Result:
    outcome: Outcome
    attempts: int
    response: httpx.Response | None = None
    error: Exception | None = None
    history: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "done" and self.response is not None and self.response.is_success


def _classify(response: httpx.Response | None, error: Exception | None) -> Outcome:
    if error is not None:
        if isinstance(error, _NOT_SENT_ERRORS):
            return "not_sent"
        if isinstance(error, _MAYBE_SENT_ERRORS):
            return "unconfirmed"
        raise error  # a programming error (bad URL, bad arguments), not a network one
    assert response is not None
    if response.status_code == 429:
        return "not_sent"  # the server says it did not process the request
    if response.status_code >= 500:
        return "unconfirmed"
    return "done"


def send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 0.2,
    idempotency_key: bool | str = False,
    **kwargs: Any,
) -> Result:
    """Send a request, retrying only when the outcome makes it safe.

    ``idempotency_key=True`` adds a random ``Idempotency-Key`` header, kept the
    same on every attempt, so a server that de-duplicates on it can safely
    receive a retried POST. Pass a string to choose the key yourself.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    method = method.upper()
    headers = dict(kwargs.pop("headers", None) or {})
    if idempotency_key:
        key = idempotency_key if isinstance(idempotency_key, str) else str(uuid.uuid4())
        headers.setdefault("Idempotency-Key", key)
    may_retry_unconfirmed = method in IDEMPOTENT_METHODS or "Idempotency-Key" in headers

    history: list[str] = []
    for attempt in range(1, attempts + 1):
        response: httpx.Response | None = None
        error: Exception | None = None
        try:
            response = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            error = exc
        outcome = _classify(response, error)
        history.append(outcome if error is None else f"{outcome}:{type(error).__name__}")

        retryable = outcome == "not_sent" or (outcome == "unconfirmed" and may_retry_unconfirmed)
        if outcome == "done" or not retryable or attempt == attempts:
            return Result(outcome, attempt, response, error, history)
        time.sleep(backoff * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")
