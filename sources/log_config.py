import logging
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

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)

    return logger