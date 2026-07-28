import logging

logger = logging.getLogger("postiq")
logger.setLevel(logging.INFO)
logger.propagate = False  # own handler below; don't duplicate via the root logger
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)


def silence_noisy_loggers() -> None:
    """Stop third-party libraries from logging our credentials.

    httpx logs the full request URL at INFO. Graph API calls carry
    ``client_secret``, ``access_token`` and ``appsecret_proof`` as query
    parameters, so at INFO every sync writes live credentials into stdout — and
    from there into terminal scrollback, log aggregators, and support tickets.

    Meta's API requires those as query params, so the fix is at the logging end.
    Raised to WARNING: real failures still surface, but the request line does
    not. Called from both the API and the workers so no entrypoint is missed.
    """
    for name in ("httpx", "httpcore", "aio_pika", "aiormq"):
        logging.getLogger(name).setLevel(logging.WARNING)


def redact(value: str | None, keep: int = 4) -> str:
    """Render a secret safe to log: ``EAAd…(len=211)``."""
    if not value:
        return "<none>"
    return f"{value[:keep]}…(len={len(value)})"
