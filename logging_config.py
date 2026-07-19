"""
Structured logging for FAWN's money-movement and fraud/compliance code
paths (services/blockchain_monitor.py, services/onchain_send.py,
services/sanctions_screening.py, services/address_risk.py,
services/crypto_wallet.py, routers/crypto.py, routers/admin_credit.py).

Scoped deliberately: this does NOT sweep every print() in the codebase
(147+ across marketing/podcast/email modules unrelated to money
movement) -- only the paths where being able to reconstruct "what
happened, for which user, in which request" later actually matters,
e.g. investigating a flagged transaction or a settlement failure.

Every log line automatically includes the request's correlation ID
(via asgi-correlation-id, bound through contextvars -- async-safe,
unlike thread-locals) so every log emitted anywhere during a single
request -- including nested calls into OFAC screening, address-risk
checks, or on-chain settlement -- can be grepped/queried together by
one ID.
"""
import logging
import sys

import structlog


def configure_logging() -> None:
    """Call once, at app startup, before any get_logger() calls that
    matter for output formatting."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
