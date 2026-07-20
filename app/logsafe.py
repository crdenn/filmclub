"""Redact secrets from log output.

Some upstream errors carry credentials in their string form — most notably
`httpx` errors, which include the full request URL, and TMDB puts the API key in
a query-string (`?api_key=...`). A single redacting filter on the root logging
handlers scrubs those before anything is written, so no call site can leak a key
by logging an exception, and future code is covered too.
"""
import logging
import re

# Query-string secrets: api_key / apikey / token / x-plex-token / secret = VALUE.
_QUERY_SECRET = re.compile(
    r"(?i)(?P<key>api[_-]?key|x-plex-token|access[_-]?token|token|secret)=(?P<val>[^&\s'\"]+)"
)
# The Plex rating webhook path embeds its shared secret as a path segment.
_WEBHOOK_PATH = re.compile(r"(/api/plex/webhook/)[^/\s'\"]+")


def redact(text: str) -> str:
    """Return `text` with known secret shapes replaced by REDACTED."""
    if not text:
        return text
    text = _QUERY_SECRET.sub(lambda m: f"{m.group('key')}=REDACTED", text)
    text = _WEBHOOK_PATH.sub(r"\1REDACTED", text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites a record's formatted message in place if it contains a secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            cleaned = redact(msg)
            if cleaned != msg:
                record.msg = cleaned
                record.args = ()
        except Exception:  # noqa: BLE001 — logging must never raise
            pass
        return True


def install() -> None:
    """Attach the redacting filter to the root handlers so every emitted record
    is scrubbed. Idempotent: won't add a second filter."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
