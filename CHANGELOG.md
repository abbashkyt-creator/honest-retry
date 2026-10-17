# Changelog

## 0.2.3 (2026-10-17)

A bug found in review of 0.2.2, with a test that fails on 0.2.2 and passes now.

- **Fixed: a redirect loop raised instead of returning an outcome.** With `follow_redirects=True`, httpx re-sent the
  POST on every 307 and then raised `TooManyRedirects`, which `send()` passed on to the caller. It is now
  `unconfirmed`: the request reached the server, maybe many times. Test:
  `test_a_redirect_loop_followed_by_httpx_is_unconfirmed_not_raised`.
- **Docs:** `follow_redirects` re-sends requests out of this library's sight.

## 0.2.2 (2026-10-17)

A bug found in review of 0.2.1, with a test that fails on 0.2.1 and passes now.

- **Fixed: `data=<iterator>` slipped past the one-shot check.** httpx 0.28 still sends a `data=` that is not a dict
  as raw content. A retried PUT with `data=<generator>` went out empty, and the call reported `done`. `data=` is now
  checked like `content=`: bytes, str or a form dict pass; anything else is refused when `attempts` is above 1. Test:
  `test_a_data_iterator_is_refused_too` (fails on 0.2.1); `test_form_data_can_still_be_retried`.
- **Changed:** `bytearray` and `memoryview` bodies are no longer treated as safe. httpx 0.28 cannot send them.

## 0.2.1 (2026-10-17)

Bugs found in review of 0.2.0. Each one has a test that fails on 0.2.0 and passes now.

- **Fixed: a retried request re-sent a streaming body empty.**
  - httpx reads an iterator, generator or file body as it sends it. On a retried PUT, a generator body went out as an
    empty chunked body. A file body failed halfway with `LocalProtocolError`, raised to the caller after the first
    attempt may already have been received.
  - With `attempts` above 1, such a body (and a non-seekable file in `files=`) is now refused with a `TypeError`
    before anything is sent. `attempts=1` still sends it.
  - Tests: `test_a_body_that_can_be_read_only_once_is_refused_before_sending` (generator and file),
    `test_a_non_seekable_file_upload_is_refused_when_retries_are_possible`.
- **Fixed: `Retry-After: ²` raised ValueError.** `"²".isdigit()` is True, but `float("²")` fails. Only ASCII digits
  are read as seconds now; anything else that is not an HTTP date falls back to the backoff. Test:
  `test_a_retry_after_that_is_not_ascii_digits_falls_back_to_backoff`.
- **Tests:** the test server now reads chunked request bodies.

## 0.2.0 (2026-10-17)

Bugs found in review of 0.1.0. Each one has a test that fails on 0.1.0 and passes now.

- **Fixed: a call could report `not_sent` after an earlier attempt had reached the server.**
  - With an Idempotency-Key, the sequence "connection reset" → "refused" → "refused" returned `not_sent`, taken from
    the last attempt alone, even though the first attempt may have been received.
  - The outcome now describes the whole call: once any attempt may have been delivered, the result is `unconfirmed`.
  - Test: `test_a_delivered_attempt_is_never_reported_as_not_sent`.
- **Fixed: a caller's `idempotency-key` header in lower case got a second key appended.** Headers are now
  case-insensitive (`httpx.Headers`), and a key the caller set is kept as it is.
  - Test: `test_a_caller_key_in_any_letter_case_is_kept_and_not_duplicated`.
- **Fixed: the generated Idempotency-Key was not returned,** so a caller's own later retry had to invent a new one.
  `Result.idempotency_key` now carries it.
  - Test: `test_auto_generated_key_is_stable_across_attempts_and_returned`.
- **Changed: a 429 is now `rejected`, not `not_sent`.** The request did reach the server, which refused it. Retries wait
  for the server's `Retry-After` (seconds or HTTP date), capped by `max_wait`.
- **Changed: more errors are classified.**
  - `httpx.ProxyError` is `not_sent`.
  - `httpx.DecodingError` is `unconfirmed`: an answer arrived but could not be read.
  - Previously both were raised.
- **Added:** jitter on backoff, and a `sleep` parameter so tests can observe waits.
- **Added tests:** read timeout, 502/503/504 for POST and GET, and the Retry-After cap.

## 0.1.0 (2026-10-17)

First release.
