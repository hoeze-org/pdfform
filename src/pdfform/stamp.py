"""Draw a signature image into a signature field.

This is not a cryptographic signature, it only makes the field look signed. The
image becomes the widget's normal appearance (``/AP /N``), so the field stays a
field, ``fill --flatten`` bakes it into the page like any other widget, and
:mod:`pdfform.sign` can sign the same field afterwards.

Every source format is first turned into a form XObject. A PDF page already is
one. A raster image becomes an image XObject drawn by a one-line content stream.
SVG is converted to a PDF page first. The form XObject is then nested in the
widget appearance, whose ``/BBox`` is the widget ``/Rect`` at the origin, so the
viewer does not scale it again.
"""

from __future__ import annotations

import io
import logging
import zlib
from pathlib import Path
from typing import Any, Literal

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from pdfform.extract import extract_form_and_objects, open_reader
from pdfform.fill import _match_field, _write_out
from pdfform.flatten import IDENTITY, _floats, _format_matrix, placement_matrix
from pdfform.model import FieldKind, FieldValueError, MissingDependencyError, SignatureImageError

logger = logging.getLogger(__name__)

Fit = Literal["contain", "stretch"]

_ROTATION_MATRIX = {
    90: (0.0, -1.0, 1.0, 0.0, 0.0, 0.0),
    180: (-1.0, 0.0, 0.0, -1.0, 0.0, 0.0),
    270: (0.0, 1.0, -1.0, 0.0, 0.0, 0.0),
}


def _sniff(data: bytes) -> Literal["pdf", "svg", "raster"]:
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if b"<svg" in data[:4096]:
        return "svg"
    return "raster"


def _require(module: str, extra: str, what: str) -> Any:
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise MissingDependencyError(
            f"{what} needs the optional dependency {module!r}: pip install 'pdfform[{extra}]'"
        ) from exc


def _number(value: float) -> Any:
    return NumberObject(int(value)) if float(value).is_integer() else FloatObject(value)


def _box(values: tuple[float, ...]) -> ArrayObject:
    return ArrayObject([_number(v) for v in values])


def _form_from_pdf(
    writer: PdfWriter, data: bytes
) -> tuple[StreamObject, tuple[float, float, float, float], tuple[float, ...]]:
    """First page of a PDF as a form XObject in *writer*, with its bbox and matrix."""
    try:
        source = PdfReader(io.BytesIO(data))
        if not source.pages:
            raise SignatureImageError("The signature PDF has no pages")
        # add_page copies the inheritable attributes (/Resources, /MediaBox, ...) onto the page.
        page = PdfWriter().add_page(source.pages[0])
        contents = page.get_contents()
        content = contents.get_data() if contents is not None else b""
        resources = page.get("/Resources")
        box = page.cropbox
        bbox = (float(box.left), float(box.bottom), float(box.right), float(box.top))
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        resource_dict = resources.get_object().clone(writer) if resources is not None else DictionaryObject()
    except SignatureImageError:
        raise
    except Exception as exc:
        raise SignatureImageError(f"Could not read the signature PDF: {exc}") from exc

    matrix = _ROTATION_MATRIX.get(rotation, IDENTITY)
    form = DecodedStreamObject()
    form.set_data(content)
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = _box(bbox)
    form[NameObject("/Resources")] = resource_dict
    if matrix != IDENTITY:
        form[NameObject("/Matrix")] = _box(matrix)
    return form, bbox, matrix


def _form_from_svg(
    writer: PdfWriter, data: bytes
) -> tuple[StreamObject, tuple[float, float, float, float], tuple[float, ...]]:
    svglib = _require("svglib.svglib", "stamp", "SVG signatures")
    render_pdf = _require("reportlab.graphics.renderPDF", "stamp", "SVG signatures")
    try:
        drawing = svglib.svg2rlg(io.BytesIO(data))
        if drawing is None:
            raise SignatureImageError("Could not parse the signature SVG")
        converted = render_pdf.drawToString(drawing)
    except SignatureImageError:
        raise
    except Exception as exc:
        raise SignatureImageError(f"Could not convert the signature SVG: {exc}") from exc
    return _form_from_pdf(writer, converted)


def _image_object(pixels: bytes, width: int, height: int, colour_space: str) -> StreamObject:
    image = DecodedStreamObject()
    image.set_data(zlib.compress(pixels))
    image[NameObject("/Type")] = NameObject("/XObject")
    image[NameObject("/Subtype")] = NameObject("/Image")
    image[NameObject("/Width")] = NumberObject(width)
    image[NameObject("/Height")] = NumberObject(height)
    image[NameObject("/ColorSpace")] = NameObject(colour_space)
    image[NameObject("/BitsPerComponent")] = NumberObject(8)
    image[NameObject("/Filter")] = NameObject("/FlateDecode")
    return image


def _form_from_raster(
    writer: PdfWriter, data: bytes
) -> tuple[StreamObject, tuple[float, float, float, float], tuple[float, ...]]:
    pil = _require("PIL.Image", "stamp", "PNG and JPEG signatures")
    ops = _require("PIL.ImageOps", "stamp", "PNG and JPEG signatures")
    try:
        picture = pil.open(io.BytesIO(data))
        picture.load()
        picture = ops.exif_transpose(picture)
        width, height = picture.size
        has_alpha = picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info
        grey = picture.mode in ("L", "LA")
        if has_alpha:
            picture = picture.convert("LA" if grey else "RGBA")
            alpha = picture.getchannel("A")
            colour = picture.convert("L" if grey else "RGB")
        else:
            alpha = None
            colour = picture.convert("L" if grey else "RGB")
        image = _image_object(colour.tobytes(), width, height, "/DeviceGray" if grey else "/DeviceRGB")
        if alpha is not None and alpha.getextrema() != (255, 255):
            mask = _image_object(alpha.tobytes(), width, height, "/DeviceGray")
            image[NameObject("/SMask")] = writer._add_object(mask)
    except Exception as exc:
        raise SignatureImageError(f"Could not read the signature image: {exc}") from exc

    form = DecodedStreamObject()
    form.set_data(f"q {width} 0 0 {height} 0 0 cm /Img Do Q".encode("ascii"))
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = _box((0, 0, width, height))
    resources = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Img"): writer._add_object(image)})}
    )
    form[NameObject("/Resources")] = resources
    return form, (0.0, 0.0, float(width), float(height)), IDENTITY


def _signature_form(
    writer: PdfWriter, data: bytes
) -> tuple[StreamObject, tuple[float, float, float, float], tuple[float, ...]]:
    kind = _sniff(data)
    if kind == "pdf":
        return _form_from_pdf(writer, data)
    if kind == "svg":
        return _form_from_svg(writer, data)
    return _form_from_raster(writer, data)


def _target(
    width: float, height: float, bbox: tuple[float, float, float, float], matrix: tuple[float, ...], fit: Fit
) -> tuple[float, float, float, float]:
    """Where inside ``(0, 0, width, height)`` the image goes."""
    if fit == "stretch":
        return (0.0, 0.0, width, height)
    a, b, c, d, e, f = matrix
    corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])]
    xs = [a * x + c * y + e for x, y in corners]
    ys = [b * x + d * y + f for x, y in corners]
    image_width, image_height = max(xs) - min(xs), max(ys) - min(ys)
    if image_width <= 0 or image_height <= 0:
        raise SignatureImageError("The signature image has no area")
    scale = min(width / image_width, height / image_height)
    fitted_width, fitted_height = image_width * scale, image_height * scale
    left, bottom = (width - fitted_width) / 2, (height - fitted_height) / 2
    return (left, bottom, left + fitted_width, bottom + fitted_height)


def stamp_signature(
    source: Any,
    field: str,
    image: Any,
    output: Any = None,
    *,
    fit: Fit = "contain",
) -> bytes:
    """Draw a signature image into a signature field and return the document as bytes.

    Args:
        source: Path, bytes, or an open ``PdfReader`` of the form.
        field: Name of the signature field. A partial name works when it is unambiguous.
        image: Path or bytes of a PDF, PNG, JPEG or SVG. The format is detected from
            the content. For a PDF, only the first page is used. Raster formats need
            ``pdfform[stamp]``, as does SVG.
        output: Optional path or writable binary stream. The bytes are returned either way.
        fit: ``contain`` keeps the aspect ratio and centres the image in the field.
            ``stretch`` fills the field and distorts the image to do so.

    Raises:
        UnknownFieldError: *field* names no field.
        FieldValueError: *field* is not a signature field, or has no widget with an area.
        SignatureImageError: The image could not be read.
        MissingDependencyError: The optional dependency for the image format is missing.
    """
    if fit not in ("contain", "stretch"):
        raise FieldValueError(f"fit must be 'contain' or 'stretch', got {fit!r}")
    data = image if isinstance(image, bytes) else Path(image).read_bytes()

    writer = PdfWriter(clone_from=open_reader(source))
    info, objects = extract_form_and_objects(writer)
    target = _match_field(field, info, {f.name: f for f in info.fields})
    if target.kind is not FieldKind.SIGNATURE:
        raise FieldValueError(f"{target.name}: is a {target.kind.value} field, only signature fields can be stamped")

    form, bbox, matrix = _signature_form(writer, data)
    form_ref = writer._add_object(form)

    stamped = 0
    for widget in objects[target.name][1]:
        rect = _floats(widget.get("/Rect"), 4)
        if rect is None:
            continue
        width, height = abs(rect[2] - rect[0]), abs(rect[3] - rect[1])
        if width <= 0 or height <= 0:
            continue
        placement = placement_matrix(bbox, matrix, _target(width, height, bbox, matrix, fit))
        appearance = DecodedStreamObject()
        appearance.set_data(f"q {_format_matrix(placement)} cm /Sig Do Q".encode("ascii"))
        appearance[NameObject("/Type")] = NameObject("/XObject")
        appearance[NameObject("/Subtype")] = NameObject("/Form")
        appearance[NameObject("/BBox")] = _box((0, 0, width, height))
        appearance[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Sig"): form_ref})}
        )
        widget[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(appearance)})
        stamped += 1

    if not stamped:
        raise FieldValueError(f"{target.name}: has no widget with an area to draw into")
    logger.info("Stamped %d widget(s) of %s", stamped, target.name)
    return _write_out(writer, output)
