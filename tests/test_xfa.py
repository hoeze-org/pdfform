from __future__ import annotations

import io

from formbuilder import read
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, TextStringObject

from pdfform import detect_xfa, extract_form, get_acroform, open_pdf, strip_xfa, strip_xfa_layer, xfa_packets
from pdfform.model import XfaKind


def acroform_of(data: bytes) -> DictionaryObject:
    return get_acroform(open_pdf(data))


def test_plain_acroform_is_detected(form_bytes):
    assert detect_xfa(acroform_of(form_bytes)) is XfaKind.NONE
    assert extract_form(form_bytes).xfa is XfaKind.NONE


def test_static_xfa_is_hybrid_because_real_fields_exist(hybrid_bytes):
    assert detect_xfa(acroform_of(hybrid_bytes)) is XfaKind.HYBRID
    assert len(extract_form(hybrid_bytes)) > 0


def test_dynamic_xfa_is_an_empty_stub(dynamic_bytes):
    """An /XFA entry with no AcroForm fields is the giveaway."""
    assert detect_xfa(acroform_of(dynamic_bytes)) is XfaKind.DYNAMIC
    assert len(extract_form(dynamic_bytes)) == 0


def test_detect_handles_a_missing_acroform():
    assert detect_xfa(None) is XfaKind.NONE


def test_packets_are_split_by_name(hybrid_bytes):
    packets = xfa_packets(acroform_of(hybrid_bytes))
    assert set(packets) == {"template", "datasets"}
    assert b"xfa-template" in packets["template"]


def test_a_single_stream_xfa_is_reported_as_xdp():
    data = _single_stream_xfa()
    assert set(xfa_packets(acroform_of(data))) == {"xdp"}


def test_no_packets_without_xfa(form_bytes):
    assert xfa_packets(acroform_of(form_bytes)) == {}


def test_strip_removes_the_layer(hybrid_bytes):
    out = strip_xfa_layer(hybrid_bytes)
    assert extract_form(out).xfa is XfaKind.NONE
    assert len(extract_form(out)) == len(extract_form(hybrid_bytes))


def test_strip_reports_whether_anything_was_there(form_bytes, hybrid_bytes):
    plain = PdfWriter(clone_from=read(form_bytes))
    assert strip_xfa(plain.root_object) is False
    hybrid = PdfWriter(clone_from=read(hybrid_bytes))
    assert strip_xfa(hybrid.root_object) is True


def test_strip_also_clears_needs_rendering(hybrid_bytes):
    writer = PdfWriter(clone_from=read(hybrid_bytes))
    writer.root_object[NameObject("/NeedsRendering")] = TextStringObject("true")
    strip_xfa(writer.root_object)
    assert "/NeedsRendering" not in writer.root_object


def test_stripping_a_document_without_a_form_is_harmless(no_form_bytes):
    assert len(strip_xfa_layer(no_form_bytes)) > 0


def _single_stream_xfa() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    packet = DecodedStreamObject()
    packet.set_data(b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'/>")
    acro = DictionaryObject()
    acro[NameObject("/Fields")] = ArrayObject()
    acro[NameObject("/XFA")] = writer._add_object(packet)
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
