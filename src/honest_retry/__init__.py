"""Retry HTTP requests only when it is safe, and report whether the server may
have acted on them.

A failed request is not always a request that did not happen. If the
connection was refused, nothing was sent and a retry is safe. If the connection
dropped after the request went out, the server may already have acted on it,
and retrying a POST can do the thing twice: charge twice, order twice, bid
twice.

``send`` classifies every attempt and retries only what is safe for the
method:

* ``not_sent``: no attempt reached the server (refused, DNS, proxy failure, no
  free connection). Safe to retry.
* ``rejected``: the server answered 429 and did not process the request. Retried
  after its ``Retry-After`` (capped).
* ``unconfirmed``: at least one attempt may have been received (reset, read
  timeout, undecodable answer, 5xx). Retried only for idempotent methods, or when
  an Idempotency-Key lets the server de-duplicate.
* ``done``: the server answered with a final status. The caller acts on it.

The outcome describes the whole call, not just the last attempt: once any
attempt may have reached the server, a later "nothing was sent" can never turn
the result back into ``not_sent``.
"""
from __future__ import annotations

import email.utils
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import httpx

__all__ = ["send", "Result", "IDEMPOTENT_METHODS"]
__version__ = "0.2.0"

Outcome = Literal["done", "not_sent", "rejected", "unconfirmed"]

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})

# Nothing reached the origin server.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)
# The request may have been (partly) delivered, or an answer arrived that could not be read.
_MAYBE_SENT_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.DecodingError,
)


@dataclass
class Result:
    outcome: Outcome
    attempts: int
    response: httpx.Response | None = None
    error: Exception | None = None
    idempotency_key: str | None = None
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
        return "rejected"
    if response.status_code >= 500:
        return "unconfirmed"
    return "done"


def _retry_after(response: httpx.Response | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date), if any."""
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 0.2,
    max_wait: float = 30.0,
    jitter: float = 0.1,
    idempotency_key: bool | str = False,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Result:
    """Send a request, retrying only when the outcome makes it safe.

    ``idempotency_key=True`` adds a random ``Idempotency-Key`` header, kept the
    same on every attempt and returned on the result, so a server that
    de-duplicates on it can safely receive a retried POST, and so your own
    later retry can reuse it. Pass a string to choose the key yourself. A key
    you already put in ``headers`` (any letter case) is kept as it is.

    Waits between attempts are ``backoff * 2**n`` plus up to ``jitter`` of that
    again, or the server's ``Retry-After`` for a 429, never longer than
    ``max_wait`` seconds.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    method = method.upper()
    headers = httpx.Headers(kwargs.pop("headers", None))
    if idempotency_key and "Idempotency-Key" not in headers:
        headers["Idempotency-Key"] = (
            idempotency_key if isinstance(idempotency_key, str) else str(uuid.uuid4())
        )
    key = headers.get("Idempotency-Key")
    may_retry_unconfirmed = method in IDEMPOTENT_METHODS or key is not None

    history: list[str] = []
    maybe_delivered = False
    for attempt in range(1, attempts + 1):
        response: httpx.Response | None = None
        error: Exception | None = None
        try:
            response = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            error = exc
        outcome = _classify(response, error)
        history.append(outcome if error is None else f"{outcome}:{type(error).__name__}")
        maybe_delivered = maybe_delivered or outcome == "unconfirmed"

        retryable = outcome in ("not_sent", "rejected") or (
            outcome == "unconfirmed" and may_retry_unconfirmed
        )
        if outcome == "done" or not retryable or attempt == attempts:
            if outcome in ("not_sent", "rejected") and maybe_delivered:
                outcome = "unconfirmed"  # an earlier attempt may have reached the server
            return Result(outcome, attempt, response, error, key, history)

        wait = backoff * (2 ** (attempt - 1))
        wait += random.uniform(0, jitter * wait)
        if outcome == "rejected":
            server_wait = _retry_after(response)
            if server_wait is not None:
                wait = server_wait
        sleep(min(wait, max_wait))
    raise AssertionError("unreachable")
