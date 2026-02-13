"""Logging configuration for Hive."""

import logging
import logging.handlers
import os
from pathlib import Path


def configure_logging() -> None:
    """Configure logging for the Hive application.

    Creates a root 'hive' logger with:
    - Console handler with formatted output
    - File handler with rotation (if not in CLI context)
    - Configurable log level via HIVE_LOG_LEVEL environment variable
    """
    # Get log level from environment, default to INFO
    log_level = os.environ.get("HIVE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    # Get or create the root hive logger
    logger = logging.getLogger("hive")
    logger.setLevel(level)

    # Don't add handlers if they already exist (avoid duplicates)
    if logger.handlers:
        return

    # Format for log messages
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Console handler - always present
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # File handler - only if not in CLI context
    # Check if we're being called from CLI by looking at parent loggers
    if not _is_cli_context():
        # Create log directory if it doesn't exist
        log_dir = Path.home() / ".hive" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "hive.log"

        # Rotating file handler (10MB max, 5 backups)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)


def _is_cli_context() -> bool:
    """Check if we're running in a CLI context where file logging should be disabled."""
    # Simple heuristic: if we're in a CLI context, we probably don't want file logging
    # This can be overridden by setting HIVE_ENABLE_FILE_LOGGING=1
    if os.environ.get("HIVE_ENABLE_FILE_LOGGING") == "1":
        return False

    # Check if any CLI-related modules are in the call stack
    import inspect

    for frame_info in inspect.stack():
        if "cli.py" in frame_info.filename:
            return True

    return False


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name under the hive hierarchy.

    Args:
        name: Logger name (will be prefixed with 'hive.' if not already)

    Returns:
        Logger instance
    """
    if not name.startswith("hive."):
        name = f"hive.{name}"

    return logging.getLogger(name)
