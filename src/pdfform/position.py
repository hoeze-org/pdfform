"""Turn a place on a page into a PDF rectangle, and put a new signature field there.

A rectangle comes in one of two units. Points (``pt``) are PDF user space: 1/72
inch from the bottom left of the page, the same numbers a ``/Rect`` holds.
Millimetres (``mm``) are measured from the top left of the page as it is shown,
so a page turned by ``/Rotate`` is measured the way it prints. That is what a
ruler on a printout gives.
"""

from __future__ import annotations

from typing import Any, Literal

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

from pdfform.model import FieldValueError

Units = Literal["pt", "mm"]
Rect = tuple[float, float, float, float]

POINTS_PER_MM = 72 / 25.4
_TOLERANCE = 0.01


def page_rotation(page: Any) -> int:
    """The ``/Rotate`` of *page*, which it may inherit from the page tree."""
    node: Any = page
    while node is not None:
        node = node.get_object()
        if "/Rotate" in node:
            return int(node["/Rotate"]) % 360
        node = node.get("/Parent")
    return 0


def _shown_to_user_space(x: float, y: float, box: Rect, rotation: int) -> tuple[float, float]:
    """Map a point measured from the top left of the page as shown to user space."""
    left, bottom, right, top = box
    if rotation == 90:  # turned clockwise, so the bottom left corner is shown at the top left
        return left + y, bottom + x
    if rotation == 180:
        return right - x, bottom + y
    if rotation == 270:
        return right - y, top - x
    return left + x, top - y


def page_rect(doc: PdfReader | PdfWriter, page: int, rect: Rect, units: Units = "pt") -> Rect:
    """Return *rect* on the page with index *page* in PDF user space, as ``(llx, lly, urx, ury)``.

    Raises:
        FieldValueError: The page does not exist, the rectangle has no area, or it
            does not lie on the page.
    """
    if units not in ("pt", "mm"):
        raise FieldValueError(f"units must be 'pt' or 'mm', got {units!r}")
    count = len(doc.pages)
    if not 0 <= page < count:
        raise FieldValueError(f"There is no page {page + 1} (index {page}), the document has {count} page(s)")
    shown = doc.pages[page]
    crop = shown.cropbox
    box = (float(crop.left), float(crop.bottom), float(crop.right), float(crop.top))

    x1, y1, x2, y2 = (float(v) for v in rect)
    if units == "mm":
        rotation = page_rotation(shown)
        x1, y1, x2, y2 = (v * POINTS_PER_MM for v in (x1, y1, x2, y2))
        (ax, ay), (bx, by) = (_shown_to_user_space(x, y, box, rotation) for x, y in ((x1, y1), (x2, y2)))
        x1, y1, x2, y2 = ax, ay, bx, by
    result = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    if result[2] - result[0] <= 0 or result[3] - result[1] <= 0:
        raise FieldValueError(f"The rectangle {_show(rect, units)} has no area")
    if (
        result[0] < box[0] - _TOLERANCE
        or result[1] < box[1] - _TOLERANCE
        or result[2] > box[2] + _TOLERANCE
        or result[3] > box[3] + _TOLERANCE
    ):
        raise FieldValueError(
            f"The rectangle {_show(rect, units)} does not lie on page {page + 1}, "
            f"which is {_page_size(box, page_rotation(shown), units)}"
        )
    return result


def _show(rect: Rect, units: Units) -> str:
    return ",".join(f"{float(v):g}{units}" for v in rect)


def _page_size(box: Rect, rotation: int, units: Units) -> str:
    """The size of the page as shown, and where it starts, in *units*."""
    width, height = box[2] - box[0], box[3] - box[1]
    if units == "mm":
        if rotation in (90, 270):
            width, height = height, width
        return f"{width / POINTS_PER_MM:.0f} x {height / POINTS_PER_MM:.0f} mm"
    return f"{box[0]:g},{box[1]:g},{box[2]:g},{box[3]:g} in points"


def add_signature_field(writer: PdfWriter, name: str, page: int, rect: Rect) -> DictionaryObject:
    """Add an empty signature field with one widget at *rect* on the page with index *page*.

    *rect* is in user space, as :func:`page_rect` returns it. On a turned page,
    the widget gets the page's rotation as ``/MK /R``, so its appearance is upright.

    Raises:
        FieldValueError: *name* is not a plain field name.
    """
    if not name or "." in name:
        raise FieldValueError(f"{name!r} cannot name a new field. Use a name without '.'")
    target: Any = writer.pages[page]
    root = writer.root_object
    if "/AcroForm" not in root:
        root[NameObject("/AcroForm")] = writer._add_object(DictionaryObject({NameObject("/Fields"): ArrayObject()}))
    acro: Any = root["/AcroForm"].get_object()
    if "/Fields" not in acro:
        acro[NameObject("/Fields")] = ArrayObject()

    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
            NameObject("/F"): NumberObject(4),  # print
            NameObject("/P"): target.indirect_reference,
        }
    )
    rotation = page_rotation(target)
    if rotation:
        widget[NameObject("/MK")] = DictionaryObject({NameObject("/R"): NumberObject(rotation)})
    ref = writer._add_object(widget)
    acro["/Fields"].get_object().append(ref)
    if "/Annots" in target:
        target["/Annots"].get_object().append(ref)
    else:
        target[NameObject("/Annots")] = ArrayObject([ref])
    return widget
