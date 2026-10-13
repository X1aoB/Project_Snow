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


def test_latency_fields_keep_only_reviewed_models_and_bounded_metrics():
    data = dict(event="public_generation_complete", request_id="synthetic_request",
                character_id="sample_character", elapsed_ms=123, stage="complete",
                provider="deepseek", model_profile="deepseek-flash", channel="text",
                history_band="short", generation_class="valid_initial", stream_version=2,
                provider_calls=1, timings_ms={"provider_first_content_ms": 42, "total": 123,
                                            "private_provider_id": "DO_NOT_LOG"})
    record = logging.LogRecord("snow.public", logging.INFO, "", 0, json.dumps(data), (), None)
    assert PublicLogFilter().filter(record)
    value = json.loads(record.getMessage())
    assert value["provider"] == "deepseek" and value["model_profile"] == "deepseek-flash"
    assert value["provider_calls"] == 1 and value["stream_version"] == 2
    assert value["timings_ms"] == {"provider_first_content_ms": 42, "total": 123}
    assert value["channel"] == "text" and value["history_band"] == "short"
    assert value["generation_class"] == "valid_initial"
    data.update(provider="private_provider_id", model_profile="DO_NOT_LOG",
                channel="DO_NOT_LOG", history_band="DO_NOT_LOG", generation_class="DO_NOT_LOG",
                provider_calls=True, stream_version=500,
                timings_ms={"total": -1, "provider_headers_ms": True, "retrieval_ms": 4_000_000})
    record = logging.LogRecord("snow.public", logging.INFO, "", 0, json.dumps(data), (), None)
    assert PublicLogFilter().filter(record)
    value = json.loads(record.getMessage())
    assert value["provider"] == "other" and value["model_profile"] == "other"
    assert value["timings_ms"] == {}
    assert "provider_calls" not in value and "stream_version" not in value
    assert "DO_NOT_LOG" not in record.getMessage() and "private_provider_id" not in record.getMessage()
