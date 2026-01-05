import logging
import uuid

from src.mcp.logging_utils import get_logger


def _has_file_handler(logger: logging.Logger) -> bool:
    return any(isinstance(handler, logging.FileHandler) for handler in logger.handlers)


def test_get_logger_in_prod_has_no_file_handler(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    logger_name = f"test_logger_prod_{uuid.uuid4().hex}"
    logger = get_logger(logger_name)
    assert not _has_file_handler(logger)


def test_get_logger_in_dev_has_file_handler(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    logger_name = f"test_logger_dev_{uuid.uuid4().hex}"
    logger = get_logger(logger_name)
    assert _has_file_handler(logger)
