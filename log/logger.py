import logging
import os
from logging.handlers import RotatingFileHandler

import colorlog

LOG_DIR = "log"
INFO_LOG = os.path.join(LOG_DIR, "bot_log.log")
ERROR_LOG = os.path.join(LOG_DIR, "error_log.log")

os.makedirs(LOG_DIR, exist_ok=True)


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return super().formatTime(record, datefmt=datefmt or "%Y-%m-%d %H:%M:%S")


CONSOLE_FORMAT = "%(log_color)s%(asctime)s | %(levelname)s | %(message)s%(reset)s"
FILE_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"


def _build_console_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = colorlog.ColoredFormatter(
        CONSOLE_FORMAT,
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
    handler.setFormatter(formatter)
    return handler


def _build_file_handler(path: str, level: int) -> logging.Handler:
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setLevel(level)
    formatter = LocalTimeFormatter(FILE_FORMAT)
    handler.setFormatter(formatter)
    return handler


logger = logging.getLogger("maxload")
logger.setLevel(logging.DEBUG)
logger.propagate = False
logger.handlers.clear()

logger.addHandler(_build_console_handler(logging.INFO))
logger.addHandler(_build_file_handler(INFO_LOG, logging.INFO))
logger.addHandler(_build_file_handler(ERROR_LOG, logging.ERROR))


def _configure_third_party_loggers() -> None:
    noisy_loggers = {
        "aiogram": logging.ERROR,
        "aiogram.event": logging.CRITICAL,
        "aiosqlite": logging.ERROR,
        "httpcore": logging.WARNING,
        "httpx": logging.WARNING,
        "asyncio": logging.WARNING,
    }

    for name, level in noisy_loggers.items():
        third_party_logger = logging.getLogger(name)
        third_party_logger.setLevel(level)
        third_party_logger.propagate = False


_configure_third_party_loggers()
