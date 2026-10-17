"""Every test talks to a real local socket: refused ports, servers that hang up
after reading the request, servers that answer 503 or 429. Each server counts
how many requests actually reached it, so "not retried" is proven by the
server, not by the library's own bookkeeping."""
from __future__ import annotations

import socket
import threading
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


@contextmanager
def server(behaviour: str):
    """behaviour: 'hangup' | '503' | '429' | '200'."""
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
                    continue  # close without answering
                status = {"503": "503 Service Unavailable", "429": "429 Too Many Requests",
                          "200": "200 OK"}[behaviour]
                conn.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode())

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", seen
    finally:
        stop.set()
        thread.join(2)
        sock.close()


def refused_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    return f"http://127.0.0.1:{port}"


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
    assert len(seen) == 1  # the server got it exactly once


def test_get_is_retried_after_a_hangup(client):
    with server("hangup") as (url, seen):
        result = send(client, "GET", url, attempts=3, backoff=0)
    assert result.outcome == "unconfirmed"
    assert result.attempts == 3
    assert len(seen) == 3


def test_post_with_an_idempotency_key_is_retried_with_the_same_key(client):
    with server("hangup") as (url, seen):
        result = send(client, "POST", url, json={}, attempts=3, backoff=0, idempotency_key="k-123")
    assert result.attempts == 3
    keys = {line.lower() for head in seen for line in head.split(b"\r\n") if line.lower().startswith(b"idempotency-key")}
    assert keys == {b"idempotency-key: k-123"}


def test_post_503_is_unconfirmed_and_not_retried(client):
    with server("503") as (url, seen):
        result = send(client, "POST", url, json={}, attempts=3, backoff=0)
    assert (result.outcome, result.attempts, len(seen)) == ("unconfirmed", 1, 1)
    assert result.response.status_code == 503


def test_get_503_is_retried(client):
    with server("503") as (url, seen):
        result = send(client, "GET", url, attempts=2, backoff=0)
    assert (result.attempts, len(seen)) == (2, 2)


def test_429_means_not_processed_so_even_post_is_retried(client):
    with server("429") as (url, seen):
        result = send(client, "POST", url, json={}, attempts=2, backoff=0)
    assert (result.outcome, result.attempts, len(seen)) == ("not_sent", 2, 2)


def test_success_is_done(client):
    with server("200") as (url, seen):
        result = send(client, "POST", url, json={}, backoff=0)
    assert result.outcome == "done" and result.ok and result.attempts == 1


def test_programming_errors_are_raised_not_classified(client):
    with pytest.raises(httpx.UnsupportedProtocol):
        send(client, "GET", "ftp://example.invalid/", backoff=0)
