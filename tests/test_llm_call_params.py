"""vigil-113 / #118: drop_params default stays, but dropped params are reported; _response_json error paths."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vigil import reviewer
from vigil.reviewer import _call_llm_with_retry, _response_json

MSGS = [{"role": "user", "content": "hi"}]
ALL_SUPPORTED = ["temperature", "response_format", "max_tokens"]


def _call(supported, **kwargs):
    """Run _call_llm_with_retry with completion and the litellm helper mocked."""
    helper = supported if callable(supported) else MagicMock(return_value=supported)
    with patch.object(reviewer, "completion") as comp, \
         patch.object(reviewer, "_record_usage"), \
         patch("litellm.get_supported_openai_params", helper):
        comp.return_value = MagicMock()
        _call_llm_with_retry(MSGS, "openai/some-model", **kwargs)
    return comp


def _drop_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "dropp" in r.getMessage().lower()]


def test_drop_params_default_true():
    comp = _call(ALL_SUPPORTED, temperature=0.2)
    assert comp.call_args.kwargs["drop_params"] is True


def test_caller_drop_params_false_not_overridden(caplog):
    caplog.set_level(logging.WARNING)
    comp = _call(["max_tokens"], temperature=0.2, drop_params=False)
    assert comp.call_args.kwargs["drop_params"] is False
    assert _drop_warnings(caplog) == []  # nothing dropped, so nothing reported


def test_unsupported_params_are_reported_by_name(caplog):
    caplog.set_level(logging.WARNING)
    _call(["max_tokens"], temperature=0.2, response_format={"type": "json_object"})
    msgs = _drop_warnings(caplog)
    assert len(msgs) == 1
    assert "openai/some-model" in msgs[0]
    assert "temperature" in msgs[0] and "response_format" in msgs[0]


def test_no_warning_when_all_params_supported(caplog):
    caplog.set_level(logging.WARNING)
    comp = _call(ALL_SUPPORTED, temperature=0.2, response_format={"type": "json_object"})
    assert comp.called
    assert _drop_warnings(caplog) == []


@pytest.mark.parametrize("helper", [MagicMock(return_value=None), MagicMock(side_effect=RuntimeError("boom"))])
def test_unknown_support_still_calls_and_does_not_warn(helper, caplog):
    caplog.set_level(logging.WARNING)
    comp = _call(helper, temperature=0.2)
    assert comp.called
    assert _drop_warnings(caplog) == []


def test_non_model_kwargs_never_reported(caplog):
    caplog.set_level(logging.WARNING)
    _call([], num_retries=3, api_key="x", timeout=30)
    for msg in _drop_warnings(caplog):
        for name in ("drop_params", "num_retries", "api_key", "timeout"):
            assert name not in msg
    assert _drop_warnings(caplog) == []


def _resp(content, finish_reason="stop"):
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def test_response_json_invalid_reports_context():
    with pytest.raises(ValueError) as ei:
        _response_json(_resp("not json at all", "length"), "Lead")
    msg = str(ei.value)
    assert msg.startswith("Lead:")
    assert "finish_reason='length'" in msg
    assert "reply_chars=15" in msg
    assert "reply_start='not json at all'" in msg


def test_response_json_none_content_is_value_error():
    with pytest.raises(ValueError) as ei:
        _response_json(_resp(None), "Spec")
    assert "reply_chars=0" in str(ei.value)


def test_response_json_valid():
    assert _response_json(_resp('{"decision": "APPROVE"}'), "x") == {"decision": "APPROVE"}
