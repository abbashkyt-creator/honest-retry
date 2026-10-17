# honest-retry

Retry HTTP requests only when it is safe, and say honestly what happened.

When an HTTP request fails, that doesn't always mean it didn't happen. If the connection was refused, nothing was sent, and retrying is safe. If the connection dropped *after* the request went out, the server may already have acted on it. Retrying a POST then charges, orders or bids twice.

Most retry helpers retry on "any connection error". This one first asks where the request failed.

```python
import httpx
from honest_retry import send

with httpx.Client(timeout=10) as client:
    result = send(client, "POST", "https://api.example.com/orders", json=order, attempts=3)

if result.outcome == "done":
    handle(result.response)               # the server answered; act on its status
elif result.outcome == "not_sent":
    show("Did not go through. Safe to try again.")
else:  # "unconfirmed"
    show("Not confirmed. Check before trying again.")
```

## The rules

| What happened | Outcome | GET / PUT / DELETE | POST / PATCH |
|---|---|---|---|
| Connection refused, DNS failure, connect timeout, no free pooled connection | `not_sent` | retried | retried |
| 429 Too Many Requests | `not_sent` | retried | retried |
| Reset or hang-up after sending, read/write timeout | `unconfirmed` | retried | **not retried** |
| 5xx | `unconfirmed` | retried | **not retried** |
| Any other status | `done` | returned | returned |

`idempotency_key=True` adds a random `Idempotency-Key` header and keeps it the same on every attempt. That lets a server which de-duplicates on the header, as payment APIs commonly do, receive a retried POST safely. With a key present, an `unconfirmed` POST is retried.

Programming errors, such as an unsupported URL scheme, are raised, not classified.

## Why

I run a system that places bids with real money. A bid sent twice costs the buyer money. A bid reported as failed when it actually went through misleads them. The proxy in that system follows these rules, and its tests check that nothing is replayed after a reset ([write-up](https://github.com/abbashkyt-creator/engineering-notes/blob/main/01-never-bid-twice.md)). This library is the same idea as a small, general helper. It was written from scratch and shares no code with that system.

## Tests

```
pip install -e ".[test]"
pytest
```

Every test talks to a real local socket: a refused port, a server that hangs up after reading the request, and servers that answer 503, 429 or 200. Each server counts the requests that actually reached it, so a claim like "not retried" is checked by the server, not by the library's own counters.

## License

MIT
