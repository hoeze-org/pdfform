from __future__ import annotations

import pytest

from pdfform import extract_form
from pdfform.model import FieldKind, XfaKind


@pytest.fixture(scope="module")
def info(form_bytes):
    return extract_form(form_bytes)


def test_finds_every_field(info):
    assert {f.name for f in info} == {
        "Name",
        "Consent",
        "Colour",
        "Country",
        "Langs",
        "Address.Street",
        "Address.City",
        "Notes",
        "Submit",
        "Locked",
        "Sign",
    }


def test_field_kinds(info):
    kinds = {f.name: f.kind for f in info}
    assert kinds["Name"] is FieldKind.TEXT
    assert kinds["Consent"] is FieldKind.CHECKBOX
    assert kinds["Colour"] is FieldKind.RADIO
    assert kinds["Country"] is FieldKind.DROPDOWN
    assert kinds["Langs"] is FieldKind.LISTBOX
    assert kinds["Submit"] is FieldKind.PUSHBUTTON
    assert kinds["Sign"] is FieldKind.SIGNATURE


def test_push_buttons_hold_no_value(info):
    assert info.by_name("Submit").kind.holds_value is False
    assert info.by_name("Name").kind.holds_value is True


def test_hierarchical_names_are_fully_qualified(info):
    assert info.by_name("Address.Street").kind is FieldKind.TEXT
    assert info.by_name("Address.City").kind is FieldKind.TEXT


def test_checkbox_on_state_is_read_from_the_widget(info):
    """The on-state is whatever is in /AP /N, and it is very often not "Yes"."""
    assert info.by_name("Consent").on_states == ["Ja"]


def test_radio_options_come_from_each_kid(info):
    assert info.by_name("Colour").on_states == ["Red", "Blue"]


def test_text_field_appearance_stream_is_not_mistaken_for_states(info):
    """/AP /N of a text field is a stream, whose dictionary keys are /BBox and
    friends. Reading those as appearance states is the classic way to invent
    on-states that do not exist."""
    name = info.by_name("Name")
    assert name.on_states == []
    assert name.widgets[0].states == ()


def test_choice_options_and_labels(info):
    country = info.by_name("Country")
    assert country.options == ["DE", "FR"]
    assert country.option_labels == {"DE": "Germany", "FR": "France"}


def test_flags(info):
    assert info.by_name("Notes").required is True
    assert info.by_name("Notes").multiline is True
    assert info.by_name("Locked").read_only is True
    assert info.by_name("Langs").multi_select is True
    assert info.by_name("Name").required is False


def test_max_length_and_tooltip(info):
    name = info.by_name("Name")
    assert name.max_length == 20
    assert name.tooltip == "Full name"
    assert name.describe() == "Full name"


def test_widget_pages(info):
    assert info.by_name("Name").pages == [0]
    assert info.by_name("Locked").pages == [1]
    assert info.by_name("Colour").pages == [0]
    assert info.n_pages == 2


def test_widget_rects_are_normalised(info):
    x0, y0, x1, y1 = info.by_name("Name").widgets[0].rect
    assert (x0, y0, x1, y1) == (50, 700, 250, 720)


def test_existing_values_are_read(info):
    assert info.by_name("Locked").value == "fixed"
    assert info.by_name("Consent").value == "Off"


def test_fillable_excludes_read_only_and_buttons(info):
    fillable = {f.name for f in info.fillable}
    assert "Locked" not in fillable
    assert "Submit" not in fillable
    assert "Name" in fillable


def test_document_without_acroform_yields_an_empty_inventory(no_form_bytes):
    info = extract_form(no_form_bytes)
    assert len(info) == 0
    assert info.xfa is XfaKind.NONE
    assert info.n_pages == 1


def test_accepts_a_path_and_bytes(form_path, form_bytes):
    assert len(extract_form(form_path)) == len(extract_form(form_bytes))


def test_by_name_raises_for_unknown(info):
    with pytest.raises(KeyError):
        info.by_name("Nope")
