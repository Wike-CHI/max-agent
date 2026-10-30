"""Redact injected secrets before they reach log handlers."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

# Values are read from the environment at log time. Names only; never defaults.
SECRET_ENV_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "MAX_AGENT_WEBHOOK_SECRET",
    "XAI_API_KEY",
    "OPENAI_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "PGPASSWORD",
)

_MIN_TOKEN_LENGTH = 8
_MIN_PASSWORD_LENGTH = 4


def password_from_database_url(url: str) -> str:
    if not url or "://" not in url:
        return ""
    return urlsplit(url).password or ""


def collect_secret_values() -> list[str]:
    found: list[str] = []
    for name in SECRET_ENV_NAMES:
        raw = os.environ.get(name, "")
        if len(raw) >= _MIN_TOKEN_LENGTH:
            found.append(raw)
    password = password_from_database_url(os.environ.get("MAX_AGENT_DATABASE_URL", ""))
    if len(password) >= _MIN_PASSWORD_LENGTH:
        found.append(password)
    # Longer secrets first so a short value cannot leave a longer one half-redacted.
    found.sort(key=len, reverse=True)
    return found


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


class SecretRedactingFilter(logging.Filter):
    """Replace known secret values in the formatted log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = collect_secret_values()
        if not secrets:
            return True
        rendered = redact(record.getMessage(), secrets)
        record.msg = rendered
        record.args = ()
        record.message = rendered
        return True


_factory_installed = False
_original_factory = logging.getLogRecordFactory()


def _redacting_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    secrets = collect_secret_values()
    if not secrets:
        return record
    if isinstance(record.msg, str):
        record.msg = redact(record.msg, secrets)
    if isinstance(record.args, dict):
        record.args = {
            key: redact(value, secrets) if isinstance(value, str) else value for key, value in record.args.items()
        }
    elif isinstance(record.args, tuple):
        record.args = tuple(redact(value, secrets) if isinstance(value, str) else value for value in record.args)
    return record


def install_secret_filter() -> SecretRedactingFilter:
    """Install redaction before log records are formatted.

    A filter on the root logger does not see records emitted on child loggers.
    The log-record factory runs for every logger, which is what keeps secrets
    out of captured test logs and process handlers.
    """
    global _factory_installed
    if not _factory_installed:
        logging.setLogRecordFactory(_redacting_factory)
        _factory_installed = True
    root = logging.getLogger()
    for existing in root.filters:
        if isinstance(existing, SecretRedactingFilter):
            return existing
    redactor = SecretRedactingFilter()
    root.addFilter(redactor)
    return redactor
