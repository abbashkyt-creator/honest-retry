# honest-retry

Retry HTTP requests only when it is safe, and report whether the server may have acted on them.

When an HTTP request fails, that doesn't always mean it didn't happen. If the connection was refused, nothing was sent, and retrying is safe. If the connection dropped *after* the request went out, the server may already have acted on it. Retrying a POST then charges, orders or bids twice.

Most retry helpers retry on "any connection error". This one first asks where the request failed.

```python
import httpx
from honest_retry import send

with httpx.Client(timeout=10) as client:
    result = send(client, "POST", "https://api.example.com/orders", json=order, attempts=3)

if result.outcome == "done":
    handle(result.response)               # the server answered; act on its status
elif result.outcome in ("not_sent", "rejected"):
    show("Did not go through. Safe to try again.")
else:  # "unconfirmed"
    show("Not confirmed. Check before trying again.")
```

## The rules

| What happened | Outcome | GET / PUT / DELETE | POST / PATCH |
|---|---|---|---|
| Connection refused, DNS failure, connect timeout, proxy failure, no free pooled connection | `not_sent` | retried | retried |
| 429 Too Many Requests | `rejected` | retried after `Retry-After` (capped) | retried after `Retry-After` (capped) |
| Reset or hang-up after sending, read/write timeout, unreadable answer | `unconfirmed` | retried | **not retried** |
| 5xx | `unconfirmed` | retried | **not retried** |
| Any other status | `done` | returned | returned |

The outcome describes the whole call, not just the last attempt. If attempt 1 may have reached the server and attempts 2 and 3 were refused, the result is `unconfirmed`, not `not_sent`.

`idempotency_key=True` adds a random `Idempotency-Key` header and keeps it the same on every attempt. That lets a server which de-duplicates on the header, as payment APIs commonly do, receive a retried POST safely. The key is returned as `result.idempotency_key`, so your own later retry can reuse it. A key you set yourself, in any letter case, is kept. With a key present, an `unconfirmed` POST is retried.

A retry needs a body that can be sent twice. An iterator, generator or file passed as `content=` or `data=` (httpx sends a `data=` that is not a dict as raw content), or a non-seekable file in `files=`, is read as it is sent, so a second attempt would find it empty. With `attempts` above 1, `send` refuses such a body with a `TypeError` before sending anything. Pass bytes, a form dict, or use `attempts=1`.

httpx's `follow_redirects=True` re-sends a request on a 307 or 308 by itself, out of this library's sight: a POST can reach the server several times inside one attempt. A redirect loop that ends in `TooManyRedirects` is reported as `unconfirmed`. For requests that must not be repeated, use a client without `follow_redirects`.

Programming errors, such as an unsupported URL scheme, are raised, not classified.

## Why

I run a system that places bids with real money. A bid sent twice costs the buyer money. A bid reported as failed when it actually went through misleads them. The proxy in that system follows these rules, and its tests check that nothing is replayed after a reset ([write-up](https://github.com/abbashkyt-creator/engineering-notes/blob/main/01-never-bid-twice.md)). This library is the same idea as a small, general helper. It was written from scratch and shares no code with that system.

## Tests

```
pip install -e ".[test]"
pytest
```

Every test talks to a real local socket: a refused port, a server that hangs up after reading the request, one that goes silent, one that stops listening after the first request, and servers that answer 502, 503, 504, 429 or 200. Each server counts the requests that actually reached it, so a claim like "not retried" is checked by the server, not by the library's own counters.

See [CHANGELOG.md](CHANGELOG.md) for the bugs fixed in each release. Each fix comes with a test that fails on the release before it. Releases are tagged, so you can run the tests against an older one (`git show v0.2.1:src/honest_retry/__init__.py`).

## How it was written

I wrote it with Claude Code as a pair programmer, and every commit carries its co-author line. All releases so far came out on the same day. The bugs they fix were found by AI code review (a Claude agent asked to break the library) and each was reproduced as a failing test before it was fixed. No one else uses it yet.

## License

MIT
