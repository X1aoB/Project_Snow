import io
import json
import logging

from backend.snow_app.public_logging import PublicLogFilter, configure_public_logger


def test_completion_visible_with_root_warning_and_private_fields_removed():
    logger = configure_public_logger()
    original = [(handler, handler.stream) for handler in logger.handlers
                if getattr(handler, "snow_public_whitelist", False)]
    sink = io.StringIO()
    for handler, _ in original:
        handler.setStream(sink)
    try:
        before = logging.getLogger().level
        configure_public_logger()
        assert len([h for h in logger.handlers if getattr(h, "snow_public_whitelist", False)]) == 1
        logger.info(json.dumps(dict(event="public_generation_complete", request_id="test_request",
                                    character_id="sample_character", elapsed_ms=12, stage="complete",
                                    terminal_error="DO_NOT_LOG provider-key=secret",
                                    exception_type="PrivateError",
                                    message="DO_NOT_LOG chat", ip="DO_NOT_LOG address")))
        value = json.loads(sink.getvalue())
        assert value["terminal_error"] is True and value["exception_type"] is True
        assert "DO_NOT_LOG" not in sink.getvalue()
        assert logging.getLogger().level == before and logger.propagate is False
    finally:
        for handler, stream in original:
            handler.setStream(stream)


def test_whitelist_rejects_arbitrary_logs_and_diagnostics():
    guard = PublicLogFilter()
    for text in ('{"event":"chat","message":"private"}', "private API key", "public_warning secret"):
        assert not guard.filter(logging.LogRecord("snow.public", logging.ERROR, "", 0, text, (), None))
    record = logging.LogRecord("snow.public", logging.WARNING, "", 0,
                               "public_lease_renewal_unavailable", (), None)
    assert guard.filter(record)
