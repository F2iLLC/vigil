"""The lead reviewer sometimes answers 'prose + JSON'; the parser must find the object."""
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
    with pytest.raises(Exception):
        _parse_json_response("just words, no object")
