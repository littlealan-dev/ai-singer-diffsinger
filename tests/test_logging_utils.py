import logging
import uuid

from src.mcp.logging_utils import (
    JsonFormatter,
    build_formatter,
    get_logger,
    LoggingContextFilter,
    set_log_context,
)


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


def test_log_format_includes_context_fields(monkeypatch):
    monkeypatch.delenv("LOG_JSON", raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    formatter = build_formatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test_path.py",
        lineno=12,
        msg="hello",
        args=(),
        exc_info=None,
        func="test_func",
    )
    set_log_context(session_id="s1", job_id="j1", user_id="user-123")
    LoggingContextFilter().filter(record)
    formatted = formatter.format(record)
    assert "session_id=" in formatted
    assert "job_id=" in formatted
    assert "user_id=" in formatted


def test_json_format_includes_context_fields(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    formatter = build_formatter()
    assert isinstance(formatter, JsonFormatter)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test_path.py",
        lineno=12,
        msg="hello",
        args=(),
        exc_info=None,
        func="test_func",
    )
    set_log_context(session_id="s1", job_id="j1", user_id="user-123")
    LoggingContextFilter().filter(record)
    formatted = formatter.format(record)
    assert '"session_id"' in formatted
    assert '"job_id"' in formatted
    assert '"user_id"' in formatted


def test_prod_env_logs_propagate_to_stdout(caplog, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    logger_name = f"test_logger_prod_emit_{uuid.uuid4().hex}"
    logger = get_logger(logger_name)
    assert not _has_file_handler(logger)
    assert logger.propagate is True

    caplog.set_level(logging.INFO)
    logger.info("prod_log_test")
    assert any(record.message == "prod_log_test" for record in caplog.records)
