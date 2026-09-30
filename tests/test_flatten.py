from __future__ import annotations

import pytest
from formbuilder import appearance, read
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from pdfform import fill_form
from pdfform.flatten import flatten_widgets, placement_matrix


def test_placement_matrix_is_identity_when_bbox_matches_rect():
    assert placement_matrix((0, 0, 10, 10), (1, 0, 0, 1, 0, 0), (0, 0, 10, 10)) == (1, 0, 0, 1, 0, 0)


def test_placement_matrix_translates_to_the_rect():
    assert placement_matrix((0, 0, 10, 10), (1, 0, 0, 1, 0, 0), (50, 60, 60, 70)) == (1, 0, 0, 1, 50, 60)


def test_placement_matrix_scales_a_mismatched_bbox():
    assert placement_matrix((0, 0, 10, 10), (1, 0, 0, 1, 0, 0), (0, 0, 20, 40)) == (2, 0, 0, 4, 0, 0)


def test_placement_matrix_accounts_for_a_bbox_away_from_the_origin():
    """The offset has to be cancelled, or the appearance lands next to its field."""
    assert placement_matrix((100, 200, 110, 210), (1, 0, 0, 1, 0, 0), (0, 0, 10, 10)) == (1, 0, 0, 1, -100, -200)


def test_placement_matrix_uses_the_bounds_of_the_rotated_bbox():
    """A /Matrix that rotates by 90 degrees swaps the effective width and height."""
    result = placement_matrix((0, 0, 10, 20), (0, 1, -1, 0, 0, 0), (0, 0, 20, 10))
    assert result == pytest.approx((1.0, 0.0, 0.0, 1.0, 20.0, 0.0))


def test_placement_matrix_survives_a_degenerate_bbox():
    assert placement_matrix((5, 5, 5, 5), (1, 0, 0, 1, 0, 0), (0, 0, 10, 10)) == (1, 0, 0, 1, -5, -5)


def test_flatten_removes_the_form_and_its_widgets(form_bytes):
    out = fill_form(form_bytes, {"Name": "Erika", "Consent": True}, flatten=True)
    reader = read(out)
    assert "/AcroForm" not in reader.trailer["/Root"]
    for page in reader.pages:
        assert not page.get("/Annots")


def test_flatten_paints_the_values_onto_the_page(form_bytes):
    out = fill_form(form_bytes, {"Name": "Erika", "Consent": True}, flatten=True)
    content = read(out).pages[0].get_contents().get_data()
    assert content.count(b"Do") >= 2
    assert b"/PdfformFm0" in content


def test_flatten_keeps_the_original_page_content(form_bytes):
    plain = read(fill_form(form_bytes, {"Name": "Erika"})).pages[0].get_contents()
    before = plain.get_data() if plain is not None else b""
    after = read(fill_form(form_bytes, {"Name": "Erika"}, flatten=True)).pages[0].get_contents().get_data()
    assert before in after


def test_flatten_leaves_non_widget_annotations_alone(form_bytes):
    writer = PdfWriter(clone_from=read(form_bytes))
    note = DictionaryObject()
    note[NameObject("/Type")] = NameObject("/Annot")
    note[NameObject("/Subtype")] = NameObject("/Text")
    note[NameObject("/Rect")] = ArrayObject([NumberObject(0)] * 4)
    writer.pages[0]["/Annots"].append(writer._add_object(note))

    flatten_widgets(writer)
    remaining = writer.pages[0]["/Annots"]
    assert len(remaining) == 1
    assert str(remaining[0].get_object()["/Subtype"]) == "/Text"


def test_flatten_can_keep_the_form_interactive(form_bytes):
    writer = PdfWriter(clone_from=read(form_bytes))
    flatten_widgets(writer, remove_form=False)
    assert "/AcroForm" in writer.root_object


def test_hidden_widgets_are_not_painted(form_bytes):
    writer = PdfWriter(clone_from=read(form_bytes))
    for ref in writer.pages[0]["/Annots"]:
        ref.get_object()[NameObject("/F")] = NumberObject(2)  # hidden
    assert flatten_widgets(writer) == 0


def test_inherited_resources_are_not_shared_between_pages():
    """Two pages inheriting one /Resources must each end up with only their own
    form XObject, and must not lose the inherited entries either.

    Adding to the inherited dictionary in place would give every page every
    other page's appearances, and the reused local names would collide.
    """
    writer = _document_with_inherited_resources()
    assert flatten_widgets(writer) == 2

    first = writer.pages[0]["/Resources"]["/XObject"]
    second = writer.pages[1]["/Resources"]["/XObject"]
    assert first is not second
    assert set(first) == set(second) == {"/Shared", "/PdfformFm0"}
    assert first.raw_get("/PdfformFm0").idnum != second.raw_get("/PdfformFm0").idnum
    assert first.raw_get("/Shared").idnum == second.raw_get("/Shared").idnum


def test_flattening_does_not_pollute_the_inherited_resources():
    writer = _document_with_inherited_resources()
    flatten_widgets(writer)
    shared = writer.root_object["/Pages"].get_object()["/Resources"]["/XObject"]
    assert set(shared) == {"/Shared"}


def _document_with_inherited_resources() -> PdfWriter:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)

    shared = DictionaryObject()
    xobjects = DictionaryObject()
    xobjects[NameObject("/Shared")] = appearance(writer)
    shared[NameObject("/XObject")] = xobjects

    pages_node = writer.root_object["/Pages"].get_object()
    pages_node[NameObject("/Resources")] = shared

    fields = ArrayObject()
    for index, page in enumerate(writer.pages):
        page.pop(NameObject("/Resources"), None)
        widget = DictionaryObject()
        widget[NameObject("/Type")] = NameObject("/Annot")
        widget[NameObject("/Subtype")] = NameObject("/Widget")
        widget[NameObject("/FT")] = NameObject("/Btn")
        widget[NameObject("/T")] = TextStringObject(f"Box{index}")
        widget[NameObject("/Rect")] = ArrayObject([NumberObject(v) for v in (10, 10, 20, 20)])
        normal = DictionaryObject()
        normal[NameObject("/On")] = appearance(writer)
        ap = DictionaryObject()
        ap[NameObject("/N")] = normal
        widget[NameObject("/AP")] = ap
        widget[NameObject("/AS")] = NameObject("/On")
        ref = writer._add_object(widget)
        page[NameObject("/Annots")] = ArrayObject([ref])
        fields.append(ref)

    acro = DictionaryObject()
    acro[NameObject("/Fields")] = fields
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro)
    return writer


def test_flattened_document_is_readable(form_bytes, tmp_path):
    out = tmp_path / "flat.pdf"
    fill_form(form_bytes, {"Name": "Erika", "Colour": "Red"}, out, flatten=True)
    reader = read(out.read_bytes())
    assert len(reader.pages) == 2
    assert reader.get_fields() in (None, {})
