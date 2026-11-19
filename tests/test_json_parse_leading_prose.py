"""The lead reviewer sometimes answers 'prose + JSON'; the parser must find the object."""
import json

import pytest

from vigil.reviewer import _parse_json_response


def test_prose_before_json_object():
    text = 'The verdict follows as JSON.\n\n{"decision":"APPROVE","summary":"ok","findings":[]}'
    assert _parse_json_response(text)["decision"] == "APPROVE"


def test_prose_before_and_after_and_nested_braces():
    text = 'Intro {not json} then {"decision":"BLOCK","findings":[{"file":"a.py"}]} trailing words'
    assert _parse_json_response(text)["decision"] == "BLOCK"


def test_fenced_and_plain_still_work():
    assert _parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_response('{"a": 1} extra') == {"a": 1}


def test_no_json_still_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response("just words, no object")


def test_truncated_outer_object_raises_not_nested_dict():
    # Cut off by max_tokens: the nested finding must never be returned as the verdict.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response('{"decision":"REQUEST_CHANGES","findings":[{"file":"a.py"}')


def test_truncated_outer_object_after_prose_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response('Verdict: {"decision":"BLOCK","findings":[{"file":"a.py","line":1}')


def test_earlier_complete_object_is_returned_first():
    # Pinned: the first complete top-level object wins. The caller rejects a
    # decision-less reply (see test_truncated_verdict_fails_closed.py).
    assert _parse_json_response('Use {} here {"decision":"REQUEST_CHANGES","findings":[]}') == {}
