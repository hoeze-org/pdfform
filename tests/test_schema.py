from __future__ import annotations

import jsonschema
import pytest

from pdffill import build_schema, current_values, extract_form
from pdffill.model import DynamicXfaError
from pdffill.schema import PDF_KEYWORD


@pytest.fixture(scope="module")
def info(form_bytes):
    return extract_form(form_bytes)


@pytest.fixture(scope="module")
def schema(info):
    return build_schema(info)


def test_schema_is_itself_valid(schema):
    jsonschema.Draft202012Validator.check_schema(schema)


def test_only_fillable_fields_are_described(schema):
    assert set(schema["properties"]) == {
        "Name",
        "Consent",
        "Colour",
        "Country",
        "Langs",
        "Address.Street",
        "Address.City",
        "Notes",
    }


def test_text_field_maps_to_a_bounded_string(schema):
    assert schema["properties"]["Name"]["type"] == "string"
    assert schema["properties"]["Name"]["maxLength"] == 20
    assert schema["properties"]["Name"]["description"] == "Full name"


def test_checkbox_maps_to_a_boolean_and_records_its_on_state(schema):
    prop = schema["properties"]["Consent"]
    assert prop["type"] == "boolean"
    assert prop[PDF_KEYWORD]["onState"] == "Ja"


def test_radio_maps_to_an_enum_of_on_states(schema):
    assert schema["properties"]["Colour"] == {
        "type": "string",
        "enum": ["Red", "Blue"],
        PDF_KEYWORD: {"field": "Colour", "kind": "radio", "pages": [0], "flags": 1 << 15},
    }


def test_dropdown_maps_to_an_enum_of_export_values(schema):
    prop = schema["properties"]["Country"]
    assert prop["enum"] == ["DE", "FR"]
    assert "Germany" in prop["description"]


def test_multi_select_list_box_maps_to_an_array(schema):
    prop = schema["properties"]["Langs"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string", "enum": ["en", "de"]}
    assert prop["uniqueItems"] is True


def test_required_fields_are_listed(schema):
    assert schema["required"] == ["Notes"]


def test_read_only_and_signature_fields_are_opt_in(info):
    assert "Locked" not in build_schema(info)["properties"]
    assert "Locked" in build_schema(info, include_read_only=True)["properties"]
    assert build_schema(info, include_read_only=True)["properties"]["Locked"]["readOnly"] is True
    assert "Sign" not in build_schema(info)["properties"]
    assert "Sign" in build_schema(info, include_signatures=True)["properties"]


def test_push_buttons_are_never_described(info):
    for variant in (build_schema(info), build_schema(info, include_read_only=True, include_signatures=True)):
        assert "Submit" not in variant["properties"]


def test_nested_mode_splits_on_dots(info):
    nested = build_schema(info, nested=True)
    assert "Address" in nested["properties"]
    assert set(nested["properties"]["Address"]["properties"]) == {"Street", "City"}
    assert "Address.Street" not in nested["properties"]
    jsonschema.Draft202012Validator.check_schema(nested)


def test_nested_mode_keeps_the_flat_name_available(info):
    nested = build_schema(info, nested=True)
    assert nested["properties"]["Address"]["properties"]["Street"][PDF_KEYWORD]["field"] == "Address.Street"


def test_a_plausible_instance_validates(schema):
    instance = {
        "Name": "Erika Muster",
        "Consent": True,
        "Colour": "Blue",
        "Country": "DE",
        "Langs": ["de"],
        "Notes": "line one\nline two",
    }
    jsonschema.validate(instance, schema)


@pytest.mark.parametrize(
    ("instance", "message"),
    [
        ({"Notes": "x", "Colour": "Green"}, "Green"),
        ({"Notes": "x", "Consent": "yes"}, "yes"),
        ({"Notes": "x", "Name": "x" * 21}, "too long"),
        ({"Notes": "x", "Unknown": 1}, "Unknown"),
        ({}, "required"),
    ],
)
def test_bad_instances_are_rejected(schema, instance, message):
    with pytest.raises(jsonschema.ValidationError) as excinfo:
        jsonschema.validate(instance, schema)
    assert message in str(excinfo.value)


def test_dynamic_xfa_has_no_schema(dynamic_bytes):
    info = extract_form(dynamic_bytes)
    with pytest.raises(DynamicXfaError, match="dynamic XFA"):
        build_schema(info)


def test_hybrid_xfa_is_flagged_but_still_described(hybrid_bytes):
    schema = build_schema(extract_form(hybrid_bytes))
    assert schema[PDF_KEYWORD]["xfa"] == "hybrid"
    assert "Name" in schema["properties"]


def test_current_values_skips_empty_fields_by_default(info):
    assert current_values(info) == {}


def test_current_values_can_include_empty_fields(info):
    values = current_values(info, include_empty=True)
    assert values["Name"] == ""
    assert values["Consent"] is False
    assert values["Langs"] == []


def test_empty_choice_fields_are_null_rather_than_an_empty_string(info):
    """ "" is not one of the options, so a template using it would not validate."""
    values = current_values(info, include_empty=True)
    assert values["Colour"] is None
    assert values["Country"] is None


def test_current_values_skips_read_only_fields_like_the_schema_does(info):
    assert "Locked" not in current_values(info, include_empty=True)
    assert current_values(info, include_empty=True, include_read_only=True)["Locked"] == "fixed"


def test_values_of_a_filled_form_validate_against_the_schema(form_bytes, schema):
    from pdffill import fill_form

    filled = fill_form(form_bytes, {"Name": "Erika", "Consent": True, "Colour": "Red", "Notes": "x"})
    jsonschema.validate(current_values(extract_form(filled)), schema)


def test_validate_accepts_good_values(schema):
    from pdffill import validate_values

    assert validate_values(schema, {"Name": "Erika", "Consent": True, "Colour": "Red", "Notes": "x"}) == []


def test_validate_reports_every_problem(schema):
    from pdffill import validate_values

    problems = validate_values(schema, {"Notes": "x", "Colour": "Green", "Name": "x" * 30})
    assert len(problems) == 2
    assert any(p.startswith("Colour:") for p in problems)
    assert any(p.startswith("Name:") for p in problems)


def test_validate_ignores_nulls_because_they_mean_clear(schema):
    from pdffill import validate_values

    assert validate_values(schema, {"Notes": "x", "Colour": None, "Name": None}) == []


def test_validate_reports_missing_required_fields(schema):
    from pdffill import validate_values

    problems = validate_values(schema, {"Name": "Erika"})
    assert len(problems) == 1
    assert "Notes" in problems[0]
