"""Every test talks to a real local socket: refused ports, servers that hang up
after reading the request, go silent, or answer 5xx/429. Each server counts
how many requests actually reached it, so "not retried" is proven by the
server, not by the library's own bookkeeping."""
from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest

from honest_retry import send


def _read_request(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        body += conn.recv(65536)
    return head


ANSWERS = {
    "200": "200 OK",
    "429": "429 Too Many Requests",
    "502": "502 Bad Gateway",
    "503": "503 Service Unavailable",
    "504": "504 Gateway Timeout",
}


@contextmanager
def server(behaviour: str, extra_headers: str = ""):
    """behaviour: a status in ANSWERS, 'hangup', 'hangup_then_close' or 'silent'."""
    seen: list[bytes] = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    stop = threading.Event()

    def loop():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            with conn:
                head = _read_request(conn)
                if not head:
                    continue
                seen.append(head)
                if behaviour == "hangup":
                    continue
                if behaviour == "hangup_then_close":
                    stop.set()
                    sock.close()  # from now on, connections are refused
                    return
                if behaviour == "silent":
                    time.sleep(1.0)
                    continue
                conn.sendall(
                    f"HTTP/1.1 {ANSWERS[behaviour]}\r\nContent-Length: 2\r\n{extra_headers}"
                    "Connection: close\r\n\r\nok".encode()
                )

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", seen
    finally:
        stop.set()
        thread.join(3)
        sock.close()


def refused_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def keys_seen(seen: list[bytes]) -> list[bytes]:
    return [
        line.split(b":", 1)[1].strip()
        for head in seen
        for line in head.split(b"\r\n")
        if line.lower().startswith(b"idempotency-key:")
    ]


@pytest.fixture()
def client():
    with httpx.Client(timeout=2.0) as c:
        yield c


def test_refused_post_is_retried_and_reported_not_sent(client):
    result = send(client, "POST", refused_url(), json={"bid": 10}, attempts=3, backoff=0)
    assert result.outcome == "not_sent"
    assert result.attempts == 3


def test_post_is_never_retried_after_the_server_hangs_up(client):
    with server("hangup") as (url, seen):
        result = send(client, "POST", url, json={"bid": 10}, attempts=3, backoff=0)
    assert result.outcome == "unconfirmed"
    assert result.attempts == 1
    assert len(seen) == 1


def test_post_is_never_retried_after_a_read_timeout():
    with httpx.Client(timeout=0.3) as quick, server("silent") as (url, seen):
        result = send(quick, "POST", url, json={}, attempts=3, backoff=0)
    assert (result.outcome, result.attempts, len(seen)) == ("unconfirmed", 1, 1)
    assert isinstance(result.error, httpx.ReadTimeout)


def test_get_is_retried_after_a_hangup(client):
    with server("hangup") as (url, seen):
        result = send(client, "GET", url, attempts=3, backoff=0)
    assert (result.outcome, result.attempts, len(seen)) == ("unconfirmed", 3, 3)


def test_a_delivered_attempt_is_never_reported_as_not_sent(client):
    # Attempt 1 reaches the server and is lost; attempts 2 and 3 are refused.
    # The last attempt alone says "not sent", but the call as a whole may have acted.
    with server("hangup_then_close") as (url, seen):
        result = send(client, "POST", url, json={}, attempts=3, backoff=0, idempotency_key=True)
    assert len(seen) == 1
    assert result.attempts == 3
    assert result.outcome == "unconfirmed"


def test_auto_generated_key_is_stable_across_attempts_and_returned(client):
    with server("hangup") as (url, seen):
        result = send(client, "POST", url, json={}, attempts=3, backoff=0, idempotency_key=True)
    keys = keys_seen(seen)
    assert len(keys) == 3 and len(set(keys)) == 1
    assert result.idempotency_key == keys[0].decode()


def test_a_caller_key_in_any_letter_case_is_kept_and_not_duplicated(client):
    with server("hangup") as (url, seen):
        send(client, "POST", url, json={}, attempts=2, backoff=0,
             headers={"idempotency-key": "mine"}, idempotency_key=True)
    assert keys_seen(seen) == [b"mine", b"mine"]


@pytest.mark.parametrize("status", ["502", "503", "504"])
def test_post_5xx_is_unconfirmed_and_not_retried(client, status):
    with server(status) as (url, seen):
        result = send(client, "POST", url, json={}, attempts=3, backoff=0)
    assert (result.outcome, result.attempts, len(seen)) == ("unconfirmed", 1, 1)


@pytest.mark.parametrize("status", ["502", "503", "504"])
def test_get_5xx_is_retried(client, status):
    with server(status) as (url, seen):
        result = send(client, "GET", url, attempts=2, backoff=0)
    assert (result.attempts, len(seen)) == (2, 2)


def test_429_is_rejected_and_retried_after_the_servers_retry_after(client):
    waits: list[float] = []
    with server("429", "Retry-After: 2\r\n") as (url, seen):
        result = send(client, "POST", url, json={}, attempts=3, backoff=0, sleep=waits.append)
    assert (result.outcome, result.attempts, len(seen)) == ("rejected", 3, 3)
    assert waits == [2.0, 2.0]


def test_retry_after_is_capped_by_max_wait(client):
    waits: list[float] = []
    with server("429", "Retry-After: 3600\r\n") as (url, seen):
        send(client, "GET", url, attempts=2, backoff=0, max_wait=5, sleep=waits.append)
    assert waits == [5]


def test_success_is_done(client):
    with server("200") as (url, seen):
        result = send(client, "POST", url, json={}, backoff=0)
    assert result.outcome == "done" and result.ok and result.attempts == 1


def test_programming_errors_are_raised_not_classified(client):
    with pytest.raises(httpx.UnsupportedProtocol):
        send(client, "GET", "ftp://example.invalid/", backoff=0)
