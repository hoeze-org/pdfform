"""Synthetic AcroForm documents used across the test suite.

Building the forms by hand rather than with a generator library is deliberate:
the interesting cases are exactly the ones a friendly generator smooths over -
a check box whose on-state is ``/Ja``, a radio group whose kids each have a
different on-state, a text field whose ``/AP /N`` is a stream and not a state
dictionary, and hierarchical field names.
"""

from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


def _rect(x0: float, y0: float, x1: float, y1: float) -> ArrayObject:
    return ArrayObject([FloatObject(v) for v in (x0, y0, x1, y1)])


def appearance(
    writer: PdfWriter,
    content: bytes = b"0 0 1 rg 0 0 10 10 re f",
    bbox: tuple[float, float, float, float] = (0, 0, 10, 10),
    matrix: tuple[float, ...] | None = None,
) -> IndirectObject:
    """Create a form XObject usable as an appearance stream."""
    stream = DecodedStreamObject()
    stream.set_data(content)
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    stream[NameObject("/FormType")] = NumberObject(1)
    stream[NameObject("/BBox")] = _rect(*bbox)
    stream[NameObject("/Resources")] = DictionaryObject()
    if matrix is not None:
        stream[NameObject("/Matrix")] = ArrayObject([FloatObject(v) for v in matrix])
    return writer._add_object(stream)


def _widget(writer: PdfWriter, rect: tuple[float, float, float, float], **extra: Any) -> DictionaryObject:
    widget = DictionaryObject()
    widget[NameObject("/Type")] = NameObject("/Annot")
    widget[NameObject("/Subtype")] = NameObject("/Widget")
    widget[NameObject("/Rect")] = _rect(*rect)
    widget[NameObject("/F")] = NumberObject(4)
    for key, value in extra.items():
        widget[NameObject(key)] = value
    return widget


def _states(writer: PdfWriter, *names: str) -> DictionaryObject:
    """An ``/AP`` entry whose ``/N`` is a state dictionary."""
    normal = DictionaryObject()
    for name in names:
        normal[NameObject(f"/{name}")] = appearance(writer)
    appearance_dict = DictionaryObject()
    appearance_dict[NameObject("/N")] = normal
    return appearance_dict


def _stream_ap(writer: PdfWriter) -> DictionaryObject:
    """An ``/AP`` entry whose ``/N`` is a plain stream, as text fields have."""
    appearance_dict = DictionaryObject()
    appearance_dict[NameObject("/N")] = appearance(writer, b"/Tx BMC EMC")
    return appearance_dict


def build_form(*, xfa: str | None = None, xfa_fields: bool = True) -> bytes:
    """Build the reference form used by most tests.

    Args:
        xfa: ``None`` for a plain AcroForm, ``"hybrid"`` or ``"dynamic"`` to add
            an XFA packet. A dynamic form also gets an empty ``/Fields`` array.
        xfa_fields: Kept for readability at the call site; ignored.
    """
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_blank_page(width=595, height=842)
    page1, page2 = writer.pages[0], writer.pages[1]

    fields: list[IndirectObject] = []
    annots1: list[IndirectObject] = []
    annots2: list[IndirectObject] = []

    # A text field, merged with its single widget, whose /AP /N is a stream.
    name = _widget(writer, (50, 700, 250, 720))
    name[NameObject("/FT")] = NameObject("/Tx")
    name[NameObject("/T")] = TextStringObject("Name")
    name[NameObject("/TU")] = TextStringObject("Full name")
    name[NameObject("/MaxLen")] = NumberObject(20)
    name[NameObject("/AP")] = _stream_ap(writer)
    name_ref = writer._add_object(name)
    fields.append(name_ref)
    annots1.append(name_ref)

    # A check box whose on-state is /Ja rather than /Yes.
    consent = _widget(writer, (50, 660, 62, 672))
    consent[NameObject("/FT")] = NameObject("/Btn")
    consent[NameObject("/T")] = TextStringObject("Consent")
    consent[NameObject("/TU")] = TextStringObject("I agree")
    consent[NameObject("/V")] = NameObject("/Off")
    consent[NameObject("/AS")] = NameObject("/Off")
    consent[NameObject("/AP")] = _states(writer, "Ja", "Off")
    consent_ref = writer._add_object(consent)
    fields.append(consent_ref)
    annots1.append(consent_ref)

    # A radio group: one widget per option, each with its own on-state.
    colour = DictionaryObject()
    colour[NameObject("/FT")] = NameObject("/Btn")
    colour[NameObject("/T")] = TextStringObject("Colour")
    colour[NameObject("/Ff")] = NumberObject(1 << 15)
    colour[NameObject("/V")] = NameObject("/Off")
    colour_ref = writer._add_object(colour)
    kids = ArrayObject()
    for index, (state, x) in enumerate((("Red", 50), ("Blue", 100))):
        kid = _widget(writer, (x, 620, x + 12, 632))
        kid[NameObject("/AP")] = _states(writer, state, "Off")
        kid[NameObject("/AS")] = NameObject("/Off")
        kid[NameObject("/Parent")] = colour_ref
        kid_ref = writer._add_object(kid)
        kids.append(kid_ref)
        annots1.append(kid_ref)
    colour[NameObject("/Kids")] = kids
    fields.append(colour_ref)

    # A dropdown whose options carry separate export values and display labels.
    country = _widget(writer, (50, 580, 250, 600))
    country[NameObject("/FT")] = NameObject("/Ch")
    country[NameObject("/T")] = TextStringObject("Country")
    country[NameObject("/Ff")] = NumberObject(1 << 17)
    country[NameObject("/Opt")] = ArrayObject(
        [
            ArrayObject([TextStringObject("DE"), TextStringObject("Germany")]),
            ArrayObject([TextStringObject("FR"), TextStringObject("France")]),
        ]
    )
    country_ref = writer._add_object(country)
    fields.append(country_ref)
    annots1.append(country_ref)

    # A multi-select list box.
    langs = _widget(writer, (50, 520, 250, 570))
    langs[NameObject("/FT")] = NameObject("/Ch")
    langs[NameObject("/T")] = TextStringObject("Langs")
    langs[NameObject("/Ff")] = NumberObject(1 << 21)
    langs[NameObject("/Opt")] = ArrayObject([TextStringObject("en"), TextStringObject("de")])
    langs_ref = writer._add_object(langs)
    fields.append(langs_ref)
    annots1.append(langs_ref)

    # A hierarchical field: the qualified names are Address.Street / Address.City.
    address = DictionaryObject()
    address[NameObject("/T")] = TextStringObject("Address")
    address_ref = writer._add_object(address)
    address_kids = ArrayObject()
    for partial, y in (("Street", 470), ("City", 440)):
        kid = _widget(writer, (50, y, 250, y + 20))
        kid[NameObject("/FT")] = NameObject("/Tx")
        kid[NameObject("/T")] = TextStringObject(partial)
        kid[NameObject("/Parent")] = address_ref
        kid_ref = writer._add_object(kid)
        address_kids.append(kid_ref)
        annots1.append(kid_ref)
    address[NameObject("/Kids")] = address_kids
    fields.append(address_ref)

    # A required, multi-line text field.
    notes = _widget(writer, (50, 360, 250, 420))
    notes[NameObject("/FT")] = NameObject("/Tx")
    notes[NameObject("/T")] = TextStringObject("Notes")
    notes[NameObject("/Ff")] = NumberObject((1 << 12) | (1 << 1))
    notes_ref = writer._add_object(notes)
    fields.append(notes_ref)
    annots1.append(notes_ref)

    # A push button, which holds no value at all.
    submit = _widget(writer, (50, 300, 150, 330))
    submit[NameObject("/FT")] = NameObject("/Btn")
    submit[NameObject("/T")] = TextStringObject("Submit")
    submit[NameObject("/Ff")] = NumberObject(1 << 16)
    submit[NameObject("/AP")] = _states(writer, "Off")
    submit_ref = writer._add_object(submit)
    fields.append(submit_ref)
    annots1.append(submit_ref)

    # On page two: a read-only field and a signature field.
    locked = _widget(writer, (50, 700, 250, 720))
    locked[NameObject("/FT")] = NameObject("/Tx")
    locked[NameObject("/T")] = TextStringObject("Locked")
    locked[NameObject("/Ff")] = NumberObject(1 << 0)
    locked[NameObject("/V")] = TextStringObject("fixed")
    locked_ref = writer._add_object(locked)
    fields.append(locked_ref)
    annots2.append(locked_ref)

    sign = _widget(writer, (50, 640, 250, 680))
    sign[NameObject("/FT")] = NameObject("/Sig")
    sign[NameObject("/T")] = TextStringObject("Sign")
    sign_ref = writer._add_object(sign)
    fields.append(sign_ref)
    annots2.append(sign_ref)

    page1[NameObject("/Annots")] = ArrayObject(annots1)
    page2[NameObject("/Annots")] = ArrayObject(annots2)

    acro = DictionaryObject()
    acro[NameObject("/Fields")] = ArrayObject([] if xfa == "dynamic" else fields)
    acro[NameObject("/DA")] = TextStringObject("/Helv 0 Tf 0 g")
    if xfa is not None:
        template = DecodedStreamObject()
        template.set_data(b"<template xmlns='http://www.xfa.org/schema/xfa-template/3.0/'/>")
        datasets = DecodedStreamObject()
        datasets.set_data(b"<xfa:datasets xmlns:xfa='http://www.xfa.org/schema/xfa-data/1.0/'/>")
        acro[NameObject("/XFA")] = ArrayObject(
            [
                TextStringObject("template"),
                writer._add_object(template),
                TextStringObject("datasets"),
                writer._add_object(datasets),
            ]
        )
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def read(data: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(data))


def field_dict(data: bytes, name: str) -> DictionaryObject:
    """Look up a top-level field of a written document by its ``/T``."""
    reader = read(data)
    acro = reader.trailer["/Root"]["/AcroForm"].get_object()
    for ref in acro["/Fields"]:
        obj = ref.get_object()
        if str(obj.get("/T")) == name:
            return obj
    raise KeyError(name)
