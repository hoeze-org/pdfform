from __future__ import annotations

import pytest
from formbuilder import field_dict, read

from pdfform import extract_form, fill_form
from pdfform.model import (
    DynamicXfaError,
    FieldValueError,
    UnknownFieldError,
    XfaKind,
)


def refill(form_bytes, values, **kwargs):
    return fill_form(form_bytes, values, **kwargs)


def values_of(data):
    from pdfform import current_values

    return current_values(extract_form(data))


def test_text_value_lands_in_the_field(form_bytes):
    out = refill(form_bytes, {"Name": "Erika"})
    assert str(field_dict(out, "Name")["/V"]) == "Erika"


def test_text_appearance_stream_is_regenerated(form_bytes):
    """A value with no appearance shows up in Acrobat and nowhere else."""
    out = refill(form_bytes, {"Name": "Erika"})
    stream = field_dict(out, "Name")["/AP"]["/N"].get_object()
    assert b"Erika" in stream.get_data()


def test_need_appearances_is_set_by_default(form_bytes):
    out = refill(form_bytes, {"Name": "Erika"})
    acro = read(out).trailer["/Root"]["/AcroForm"].get_object()
    assert acro["/NeedAppearances"].value is True


def test_need_appearances_can_be_turned_off(form_bytes):
    out = refill(form_bytes, {"Name": "Erika"}, need_appearances=False)
    acro = read(out).trailer["/Root"]["/AcroForm"].get_object()
    assert acro["/NeedAppearances"].value is False


@pytest.mark.parametrize("value", [True, "true", "yes", "Ja", "ja", "X", 1])
def test_checkbox_truthy_values_select_the_real_on_state(form_bytes, value):
    out = refill(form_bytes, {"Consent": value})
    consent = field_dict(out, "Consent")
    assert str(consent["/V"]) == "/Ja"
    assert str(consent["/AS"]) == "/Ja"


@pytest.mark.parametrize("value", [False, "false", "no", "Off", "", None])
def test_checkbox_falsy_values_go_to_off(form_bytes, value):
    out = refill(form_bytes, {"Consent": value})
    consent = field_dict(out, "Consent")
    assert str(consent["/V"]) == "/Off"
    assert str(consent["/AS"]) == "/Off"


def test_checkbox_value_is_a_name_not_a_string(form_bytes):
    """A button value written as a text string is silently ignored by viewers."""
    from pypdf.generic import NameObject

    out = refill(form_bytes, {"Consent": True})
    assert isinstance(field_dict(out, "Consent")["/V"], NameObject)


def test_checkbox_rejects_nonsense(form_bytes):
    with pytest.raises(FieldValueError, match="check box value"):
        refill(form_bytes, {"Consent": "maybe"})


def test_radio_sets_the_group_value_and_every_widget_state(form_bytes):
    out = refill(form_bytes, {"Colour": "Blue"})
    colour = field_dict(out, "Colour")
    assert str(colour["/V"]) == "/Blue"
    states = [str(kid.get_object()["/AS"]) for kid in colour["/Kids"]]
    assert states == ["/Off", "/Blue"]


def test_radio_switching_turns_the_other_option_off(form_bytes):
    once = refill(form_bytes, {"Colour": "Blue"})
    twice = refill(once, {"Colour": "Red"})
    colour = field_dict(twice, "Colour")
    assert str(colour["/V"]) == "/Red"
    assert [str(kid.get_object()["/AS"]) for kid in colour["/Kids"]] == ["/Red", "/Off"]


def test_radio_can_be_cleared(form_bytes):
    out = refill(refill(form_bytes, {"Colour": "Blue"}), {"Colour": None})
    colour = field_dict(out, "Colour")
    assert str(colour["/V"]) == "/Off"
    assert {str(kid.get_object()["/AS"]) for kid in colour["/Kids"]} == {"/Off"}


def test_radio_is_case_insensitive(form_bytes):
    assert str(field_dict(refill(form_bytes, {"Colour": "blue"}), "Colour")["/V"]) == "/Blue"


def test_radio_rejects_an_option_that_does_not_exist(form_bytes):
    with pytest.raises(FieldValueError, match="Red, Blue"):
        refill(form_bytes, {"Colour": "Green"})


def test_dropdown_accepts_the_export_value(form_bytes):
    out = refill(form_bytes, {"Country": "FR"})
    assert str(field_dict(out, "Country")["/V"]) == "FR"


def test_dropdown_accepts_the_display_label(form_bytes):
    out = refill(form_bytes, {"Country": "Germany"})
    assert str(field_dict(out, "Country")["/V"]) == "DE"


def test_dropdown_rejects_an_unknown_option_when_strict(form_bytes):
    with pytest.raises(FieldValueError, match="DE, FR"):
        refill(form_bytes, {"Country": "Spain"})


def test_dropdown_forces_an_unknown_option_when_not_strict(form_bytes):
    out = refill(form_bytes, {"Country": "Spain"}, strict=False)
    assert str(field_dict(out, "Country")["/V"]) == "Spain"


def test_list_box_takes_a_list(form_bytes):
    out = refill(form_bytes, {"Langs": ["en", "de"]})
    assert [str(v) for v in field_dict(out, "Langs")["/V"]] == ["en", "de"]


def test_hierarchical_field_by_qualified_name(form_bytes):
    out = refill(form_bytes, {"Address.Street": "Ismaninger Str. 22"})
    assert values_of(out)["Address.Street"] == "Ismaninger Str. 22"


def test_hierarchical_field_from_a_nested_object(form_bytes):
    out = refill(form_bytes, {"Address": {"Street": "Ismaninger Str. 22", "City": "München"}})
    assert values_of(out)["Address.City"] == "München"


def test_unqualified_name_works_when_unambiguous(form_bytes):
    out = refill(form_bytes, {"Street": "Ismaninger Str. 22"})
    assert values_of(out)["Address.Street"] == "Ismaninger Str. 22"


def test_unknown_field_is_an_error_when_strict(form_bytes):
    with pytest.raises(UnknownFieldError, match="Nope"):
        refill(form_bytes, {"Nope": "x"})


def test_unknown_field_is_skipped_when_not_strict(form_bytes):
    out = refill(form_bytes, {"Nope": "x", "Name": "Erika"}, strict=False)
    assert values_of(out) == {"Name": "Erika"}


def test_max_length_is_enforced_when_strict(form_bytes):
    with pytest.raises(FieldValueError, match="21 characters"):
        refill(form_bytes, {"Name": "x" * 21})


def test_max_length_is_only_a_warning_when_not_strict(form_bytes, caplog):
    out = refill(form_bytes, {"Name": "x" * 21}, strict=False)
    assert len(values_of(out)["Name"]) == 21


def test_signature_fields_cannot_be_filled(form_bytes):
    with pytest.raises(FieldValueError, match="signature"):
        refill(form_bytes, {"Sign": "Erika"})


def test_push_buttons_cannot_be_filled(form_bytes):
    with pytest.raises(FieldValueError, match="hold no value"):
        refill(form_bytes, {"Submit": "x"})


def test_values_must_be_an_object(form_bytes):
    with pytest.raises(FieldValueError, match="Expected an object"):
        refill(form_bytes, ["Name", "Erika"])


def test_clearing_a_text_field(form_bytes):
    out = refill(refill(form_bytes, {"Name": "Erika"}), {"Name": None})
    assert "Name" not in values_of(out)


def test_full_round_trip(form_bytes):
    payload = {
        "Name": "Erika",
        "Consent": True,
        "Colour": "Red",
        "Country": "FR",
        "Address.City": "München",
        "Notes": "two\nlines",
    }
    assert values_of(refill(form_bytes, payload)) == payload


def test_writes_to_a_path_and_returns_the_bytes(form_bytes, tmp_path):
    out = tmp_path / "filled.pdf"
    data = fill_form(form_bytes, {"Name": "Erika"}, out)
    assert out.read_bytes() == data
    assert values_of(out.read_bytes())["Name"] == "Erika"


def test_untouched_fields_are_left_alone(form_bytes):
    out = refill(form_bytes, {"Name": "Erika"})
    assert values_of(out) == {"Name": "Erika"}


def test_dynamic_xfa_cannot_be_filled(dynamic_bytes):
    with pytest.raises(DynamicXfaError, match="dynamic XFA"):
        fill_form(dynamic_bytes, {"Name": "Erika"}, strict=False)


def test_static_xfa_layer_is_dropped_by_default(hybrid_bytes):
    """Left in place, Acrobat re-reads the XFA datasets and discards the fill."""
    out = fill_form(hybrid_bytes, {"Name": "Erika"})
    assert extract_form(out).xfa is XfaKind.NONE
    assert values_of(out)["Name"] == "Erika"


def test_static_xfa_layer_can_be_kept(hybrid_bytes):
    out = fill_form(hybrid_bytes, {"Name": "Erika"}, strip_xfa=False)
    assert extract_form(out).xfa is XfaKind.HYBRID


def test_read_only_fields_are_written_with_a_warning(form_bytes, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="pdfform.fill"):
        out = refill(form_bytes, {"Locked": "changed"})
    assert "read-only" in caplog.text
    assert str(field_dict(out, "Locked")["/V"]) == "changed"
