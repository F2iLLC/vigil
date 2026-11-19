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


def test_unmatched_prose_brace_before_verdict():
    # vigil #135: an unclosed brace in prose is not a truncated JSON object.
    text = 'Context {placeholder then verdict: {"decision":"APPROVE","findings":[]}'
    assert _parse_json_response(text) == {"decision": "APPROVE", "findings": []}


def test_unmatched_prose_brace_before_request_changes_returns_outer():
    text = 'See {x and then {"decision":"REQUEST_CHANGES","findings":[{"file":"a.py","line":1}]}'
    assert _parse_json_response(text) == {
        "decision": "REQUEST_CHANGES", "findings": [{"file": "a.py", "line": 1}],
    }


def test_unmatched_prose_brace_then_truncated_verdict_raises():
    # Anti-case: skipping the prose brace must not reach the nested finding.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response('Context {placeholder then {"decision":"REQUEST_CHANGES","findings":[{"file":"a.py"}')


def test_balanced_malformed_candidate_before_verdict():
    # vigil #137
    text = 'Note {not: valid, json} then {"decision":"BLOCK","findings":[]}'
    assert _parse_json_response(text) == {"decision": "BLOCK", "findings": []}


def test_unmatched_brace_with_quoted_key_colon_raises():
    # vigil #141: an unclosed object-like brace must never fall through to a nested verdict.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            '{\'decision\': \'REQUEST_CHANGES\', "nested": {"decision":"APPROVE","findings":[]}'
        )


def test_unmatched_brace_with_unquoted_key_colon_raises():
    # vigil #141: a bare word followed by ':' starts an object, not prose.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            '{decision: "REQUEST_CHANGES", "nested": {"decision":"APPROVE","findings":[]}'
        )


def test_unmatched_brace_followed_by_object_raises():
    # vigil #141
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response('Verdict { {"decision":"APPROVE","findings":[]}')


def test_unmatched_brace_followed_by_digit_raises():
    # vigil #141
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response('{1, "nested": {"decision":"APPROVE","findings":[]}')


def test_unmatched_brace_bare_word_then_quoted_value_raises():
    # vigil #147: reviewer's example; only the first word looked like prose.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            '{decision "REQUEST_CHANGES", "nested": {"decision":"APPROVE","findings":[]}'
        )


def test_unmatched_brace_bare_word_then_quoted_key_raises():
    # vigil #147
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            '{note "decision": "REQUEST_CHANGES", "x": {"decision":"APPROVE","findings":[]}'
        )


def test_unmatched_brace_bare_word_then_comma_raises():
    # vigil #147: no quotes in the outer part, but a comma is a JSON token.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            '{decision REQUEST_CHANGES, nested: {"decision":"APPROVE","findings":[]}'
        )


def test_unmatched_brace_bare_word_then_single_quote_raises():
    # vigil #147
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            "{decision 'REQUEST_CHANGES' {\"decision\":\"APPROVE\",\"findings\":[]}"
        )


def test_unmatched_prose_brace_with_quote_fails_closed():
    # vigil #147: deliberate trade-off. A quote after an unclosed brace is
    # treated as object-like on purpose: failing closed beats recovering.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(
            'Context {placeholder "x" then {"decision":"APPROVE","findings":[]}'
        )


@pytest.mark.parametrize(
    "text",
    [
        # vigil #152: the reviewer's example, verbatim.
        '{decision REQUEST_CHANGES then {"decision":"APPROVE","findings":[]}',
        # value only
        '{REQUEST_CHANGES because the diff is unsafe {"decision":"APPROVE","findings":[]}',
        # field name later in the span
        '{verdict follows with findings below {"decision":"APPROVE","findings":[]}',
        '{BLOCK {"decision":"APPROVE","findings":[]}',
    ],
)
def test_unmatched_brace_then_verdict_word_raises(text):
    # vigil #152: a verdict word after an unclosed brace makes it object-like.
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response(text)


def test_unmatched_prose_brace_partial_verdict_words_recovers():
    # vigil #152: whole words only -- 'approved' and 'blocker' are not verdict words.
    text = 'Note {approved-looking blocker text then {"decision":"REQUEST_CHANGES","findings":[]}'
    assert _parse_json_response(text) == {"decision": "REQUEST_CHANGES", "findings": []}
