"""Bake widget appearances into the page content and drop the form.

``pypdf`` can paint an appearance stream onto a page, but it only translates it
to the annotation rectangle's lower-left corner. That is fine for the streams it
generates itself and wrong for the ones already in the document, whose ``/BBox``
is not necessarily at the origin and which may carry a ``/Matrix``.

This module implements the placement algorithm from PDF 32000-1 section 12.5.5:
map the ``/BBox``, transformed by ``/Matrix``, onto the annotation ``/Rect``.
"""

from __future__ import annotations

import logging
from typing import Any

from pypdf import PageObject, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    IndirectObject,
    NameObject,
    StreamObject,
)

logger = logging.getLogger(__name__)

ANNOT_HIDDEN = 1 << 1
ANNOT_NOVIEW = 1 << 5

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _floats(raw: Any, count: int) -> tuple[float, ...] | None:
    if raw is None:
        return None
    raw = raw.get_object()
    try:
        values = tuple(float(v.get_object()) for v in raw)
    except (TypeError, ValueError):
        return None
    return values if len(values) == count else None


def placement_matrix(
    bbox: tuple[float, float, float, float],
    matrix: tuple[float, ...],
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    """Matrix that maps a form XObject's transformed bounding box onto *rect*.

    Follows PDF 32000-1 section 12.5.5: transform the four corners of ``/BBox``
    by ``/Matrix``, take the bounding box of the result, and compute the scale
    and translation that make it coincide with the annotation rectangle. The
    form's own ``/Matrix`` is applied by the ``Do`` operator and must not be
    folded in here.
    """
    a, b, c, d, e, f = matrix
    corners = [
        (bbox[0], bbox[1]),
        (bbox[2], bbox[1]),
        (bbox[2], bbox[3]),
        (bbox[0], bbox[3]),
    ]
    transformed = [(a * x + c * y + e, b * x + d * y + f) for x, y in corners]
    bx0 = min(p[0] for p in transformed)
    bx1 = max(p[0] for p in transformed)
    by0 = min(p[1] for p in transformed)
    by1 = max(p[1] for p in transformed)

    rx0, ry0, rx1, ry1 = rect
    sx = (rx1 - rx0) / (bx1 - bx0) if bx1 > bx0 else 1.0
    sy = (ry1 - ry0) / (by1 - by0) if by1 > by0 else 1.0
    return (sx, 0.0, 0.0, sy, rx0 - bx0 * sx, ry0 - by0 * sy)


def _is_visible(widget: DictionaryObject) -> bool:
    raw = widget.get("/F")
    if raw is None:
        return True
    try:
        flags = int(raw.get_object())
    except (TypeError, ValueError):
        return True
    return not (flags & (ANNOT_HIDDEN | ANNOT_NOVIEW))


def _appearance_stream(widget: DictionaryObject) -> StreamObject | None:
    """The appearance stream a viewer would draw for this widget right now."""
    appearance = widget.get("/AP")
    if appearance is None:
        return None
    appearance = appearance.get_object()
    if not isinstance(appearance, DictionaryObject):
        return None
    normal = appearance.get("/N")
    if normal is None:
        return None
    normal = normal.get_object()
    if isinstance(normal, StreamObject):
        return normal
    if not isinstance(normal, DictionaryObject):
        return None

    state = widget.get("/AS")
    if state is None:
        parent = widget.get("/Parent")
        state = parent.get_object().get("/V") if parent is not None else None
    if state is None:
        return None
    chosen = normal.get(NameObject(str(state)))
    if chosen is None:
        return None
    chosen = chosen.get_object()
    return chosen if isinstance(chosen, StreamObject) else None


def _page_xobjects(page: PageObject) -> DictionaryObject:
    """Return an ``/XObject`` dictionary that belongs to this page alone.

    Two hazards to avoid. A page without ``/Resources`` inherits one from the
    page tree, so writing a fresh empty dictionary there would shadow the
    inherited resources and blank out the existing content. And an inherited
    ``/Resources`` is shared by every page under that node, so adding entries to
    it in place would leak one page's form XObjects into all its siblings.
    Both are handled by copying the shared dictionaries down onto the page.
    """
    resources = page.get("/Resources")
    if resources is None:
        inherited: DictionaryObject | None = None
        parent = page.get("/Parent")
        while parent is not None:
            node = parent.get_object()
            if "/Resources" in node:
                inherited = node["/Resources"].get_object()
                break
            parent = node.get("/Parent")
        resources = DictionaryObject()
        if inherited is not None:
            resources.update(inherited)
        page[NameObject("/Resources")] = resources
    else:
        resources = resources.get_object()

    xobjects = resources.get("/XObject")
    owned = DictionaryObject()
    if xobjects is not None:
        owned.update(xobjects.get_object())
    resources[NameObject("/XObject")] = owned
    return owned


def _reference(writer: PdfWriter, obj: StreamObject) -> IndirectObject:
    reference = getattr(obj, "indirect_reference", None)
    if reference is not None:
        return reference
    return writer._add_object(obj)


def _format_matrix(matrix: tuple[float, float, float, float, float, float]) -> str:
    return " ".join(f"{value:.6f}".rstrip("0").rstrip(".") or "0" for value in matrix)


def flatten_widgets(writer: PdfWriter, *, remove_form: bool = True) -> int:
    """Draw every widget annotation into its page and remove the interactive form.

    Widgets that are hidden, or that have no appearance stream to draw, are
    dropped without being painted, which is what a viewer shows for them anyway.

    Args:
        writer: The document to modify, in place.
        remove_form: Also delete the ``/AcroForm`` entry from the catalog. Turn
            this off to keep a still-interactive form that additionally has its
            current state painted onto the page.

    Returns:
        The number of widgets that were painted.
    """
    painted = 0
    for page in writer.pages:
        annots_raw = page.get("/Annots")
        if annots_raw is None:
            continue
        annots = annots_raw.get_object()
        if not isinstance(annots, (ArrayObject, list)):
            continue

        keep: list[Any] = []
        commands: list[str] = []
        xobjects: DictionaryObject | None = None
        counter = 0

        for ref in list(annots):
            widget = ref.get_object() if isinstance(ref, IndirectObject) else ref
            if not isinstance(widget, DictionaryObject) or widget.get("/Subtype") != "/Widget":
                keep.append(ref)
                continue

            stream = _appearance_stream(widget) if _is_visible(widget) else None
            rect = _floats(widget.get("/Rect"), 4)
            bbox = _floats(stream.get("/BBox"), 4) if stream is not None else None
            if stream is None or rect is None or bbox is None:
                continue  # nothing to draw, and the widget still goes away

            if xobjects is None:
                xobjects = _page_xobjects(page)

            name = NameObject(f"/PdfformFm{counter}")
            while name in xobjects:
                counter += 1
                name = NameObject(f"/PdfformFm{counter}")
            xobjects[name] = _reference(writer, stream)
            counter += 1

            normalised = (min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3]))
            matrix = _floats(stream.get("/Matrix"), 6) or IDENTITY
            placement = placement_matrix((bbox[0], bbox[1], bbox[2], bbox[3]), matrix, normalised)
            commands.append(f"q {_format_matrix(placement)} cm {name} Do Q")
            painted += 1

        if commands:
            _append_content(page, commands)
        if keep:
            page[NameObject("/Annots")] = ArrayObject(keep)
        else:
            page.pop(NameObject("/Annots"), None)

    if remove_form:
        writer.root_object.pop(NameObject("/AcroForm"), None)
    return painted


def _append_content(page: PageObject, commands: list[str]) -> None:
    """Append drawing commands to a page, isolating the existing content.

    The original content is wrapped in ``q``/``Q`` so that a stream which leaves
    the graphics state dirty - an unbalanced ``cm``, a leftover clip path -
    cannot displace or hide the appearances drawn after it.
    """
    existing = page.get_contents()
    data = existing.get_data() if existing is not None else b""
    payload = b"q\n" + data + b"\nQ\n" + ("\n".join(commands) + "\n").encode("latin-1")
    stream = ContentStream(None, None)
    stream.set_data(payload)
    page.replace_contents(stream)
