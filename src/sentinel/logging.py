"""structlog JSON logging setup. Use get_logger(); never print() in src/."""

from __future__ import annotations

import logging
import sys

import structlog


class _StderrLogger:
    """Writes each JSON line to the CURRENT sys.stderr.

    structlog's PrintLogger keeps the stream it was created with; if that stream is later
    replaced and closed (test runners, notebooks), logging would raise. Looking it up per
    call avoids that.
    """

    def msg(self, message: str) -> None:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()

    log = debug = info = warn = warning = error = err = fatal = exception = critical = msg


def _stderr_logger_factory(*_args: object) -> _StderrLogger:
    return _StderrLogger()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
