import sys
from pathlib import Path
from loguru import logger


def setup_logger(log_file: str = "logs/bot.log", level: str = "INFO", console: bool = True) -> None:
    logger.remove()

    if console:
        # Console: human-readable
        logger.add(
            sys.stderr,
            level=level,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            colorize=True,
        )

    # File: structured, rotated daily
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_file,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation="00:00",
        retention="30 days",
        compression="gz",
    )
