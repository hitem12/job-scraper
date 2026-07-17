import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def log_config(name: str = "myapp",
    log_file: str | Path = "app.log",
    level: int = logging.INFO,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    console: bool = True,
) -> logging.Logger:
    """Configure and return a logger with rotating file output.

    Safe to call multiple times (won't duplicate handlers).
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if called more than once
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    log_path = Path(log_file)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count
        )
    except OSError as exc:
        # e.g. running locally without the /var/log/<name> dir the installer
        # creates in production — fall back rather than crashing on import.
        fallback = Path.home() / ".local" / "state" / name / log_path.name
        fallback.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"Warning: cannot write to {log_path} ({exc}); logging to {fallback} instead.",
            file=sys.stderr,
        )
        file_handler = RotatingFileHandler(
            fallback, maxBytes=max_bytes, backupCount=backup_count
        )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

    return logger