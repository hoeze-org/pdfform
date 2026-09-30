"""Shared fixtures. The documents themselves are built in :mod:`formbuilder`."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from formbuilder import build_form
from pypdf import PdfWriter


@pytest.fixture(scope="session")
def form_bytes() -> bytes:
    """A plain AcroForm covering every field kind."""
    return build_form()


@pytest.fixture
def form_path(tmp_path: Path, form_bytes: bytes) -> Path:
    path = tmp_path / "form.pdf"
    path.write_bytes(form_bytes)
    return path


@pytest.fixture(scope="session")
def hybrid_bytes() -> bytes:
    """A static XFA form: real AcroForm fields plus an XFA packet."""
    return build_form(xfa="hybrid")


@pytest.fixture(scope="session")
def dynamic_bytes() -> bytes:
    """A dynamic XFA form: an XFA packet and an empty /Fields array."""
    return build_form(xfa="dynamic")


@pytest.fixture(scope="session")
def no_form_bytes() -> bytes:
    """A document with no AcroForm at all."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
