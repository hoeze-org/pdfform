"""Derive a JSON Schema from a PDF form, and fill the form from JSON.

    >>> from pdfform import extract_form, build_schema, fill_form
    >>> info = extract_form("form.pdf")
    >>> schema = build_schema(info)
    >>> fill_form("form.pdf", {"Name": "Doe", "Agreed": True}, "filled.pdf")

The command line equivalent is ``pdfform schema`` and ``pdfform fill``.
"""

from __future__ import annotations

from pdfform.extract import extract_form, extract_form_and_objects, get_acroform, open_pdf
from pdfform.fill import fill_form, flatten_values, strip_xfa_layer
from pdfform.flatten import flatten_widgets
from pdfform.model import (
    DynamicXfaError,
    FieldKind,
    FieldValueError,
    FormField,
    FormInfo,
    PdfFormError,
    UnknownFieldError,
    Widget,
    XfaKind,
)
from pdfform.schema import build_schema, current_values, field_schema, validate_values
from pdfform.xfa import detect_xfa, strip_xfa, xfa_packets

try:  # pragma: no cover - only missing when running from a source tree
    from importlib.metadata import version

    __version__ = version("pdfform")
except Exception:  # pragma: no cover
    __version__ = "0.2.0"

__all__ = [
    "DynamicXfaError",
    "FieldKind",
    "FieldValueError",
    "FormField",
    "FormInfo",
    "PdfFormError",
    "UnknownFieldError",
    "Widget",
    "XfaKind",
    "build_schema",
    "current_values",
    "detect_xfa",
    "extract_form",
    "extract_form_and_objects",
    "field_schema",
    "fill_form",
    "flatten_values",
    "flatten_widgets",
    "get_acroform",
    "open_pdf",
    "strip_xfa",
    "strip_xfa_layer",
    "validate_values",
    "xfa_packets",
    "__version__",
]
