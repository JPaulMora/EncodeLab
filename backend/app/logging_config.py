"""Force app logs to stdout — uvicorn/alembic often reset root to WARNING."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
_CONFIGURED = False


class _FlushStreamHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def setup_logging(log_file: Path | None = None) -> None:
    """Attach INFO handlers to the ``app`` logger tree (and root).

    Call at import and again in the FastAPI lifespan so configuration survives
    uvicorn / alembic reconfiguring the root logger.
    """
    global _CONFIGURED

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    stdout = _FlushStreamHandler(sys.stdout)
    stdout.setLevel(logging.INFO)
    stdout.setFormatter(formatter)

    handlers: list[logging.Handler] = [stdout]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.INFO)
        fh.setFormatter(formatter)
        handlers.append(fh)

    # Root: keep INFO so anything propagating still shows
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.INFO)

    # Dedicated app logger — does not depend on root level after uvicorn resets it
    app_log = logging.getLogger("app")
    app_log.handlers.clear()
    for h in handlers:
        app_log.addHandler(h)
    app_log.setLevel(logging.INFO)
    app_log.propagate = False

    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
    ):
        logging.getLogger(name).setLevel(logging.INFO)

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.INFO)

    _CONFIGURED = True
    logging.getLogger("app").info("Logging configured (stdout%s)", "+file" if log_file else "")
