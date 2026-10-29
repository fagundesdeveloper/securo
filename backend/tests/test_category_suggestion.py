"""The defensive half of the model suggestion step.

The model is asked for strict JSON and routinely answers with a code fence,
a sentence of preamble, an invented category or a row number that was never
sent. Each of those has to be dropped rather than letting a guess reach a
financial record, so the parser is where the tests go.
"""
from app.services.category_suggestion_service import _parse_mapping

VALID = {"Mercado", "Transporte", "Saúde", "Educação"}


def test_plain_object():
    assert _parse_mapping('{"1": "Mercado", "2": "Transporte"}', VALID) == {
        1: "Mercado",
        2: "Transporte",
    }


def test_object_wrapped_in_a_code_fence_and_prose():
    raw = 'Claro! Aqui está:\n```json\n{"3": "Saúde"}\n```\nEspero ter ajudado.'
    assert _parse_mapping(raw, VALID) == {3: "Saúde"}


def test_invented_category_is_dropped():
    """A name outside the workspace's own list is a hallucination, not a hint."""
    assert _parse_mapping('{"1": "Mercado", "2": "Criptomoedas"}', VALID) == {1: "Mercado"}


def test_non_numeric_key_is_dropped():
    assert _parse_mapping('{"um": "Mercado", "2": "Saúde"}', VALID) == {2: "Saúde"}


def test_non_string_value_is_dropped():
    assert _parse_mapping('{"1": 42, "2": "Mercado"}', VALID) == {2: "Mercado"}


def test_no_json_at_all():
    assert _parse_mapping("Não consegui classificar nenhum deles.", VALID) == {}


def test_broken_json():
    assert _parse_mapping('{"1": "Mercado",,}', VALID) == {}


def test_a_json_array_is_not_a_mapping():
    assert _parse_mapping('["Mercado", "Transporte"]', VALID) == {}


def test_empty_reply():
    assert _parse_mapping("", VALID) == {}
    assert _parse_mapping("{}", VALID) == {}
