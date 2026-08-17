"""Read the field inventory of an AcroForm out of a PDF.

The entry point is :func:`extract_form`. Everything else here deals with the
awkward parts of the AcroForm data model:

* fields form a tree, and the name of a field is the ``/T`` entries of the whole
  chain joined with ``.``;
* ``/FT``, ``/Ff``, ``/V``, ``/DV``, ``/Opt`` and ``/MaxLen`` are inheritable, so
  a terminal field may declare none of them itself;
* the widget annotations that are actually drawn are usually the kids of a
  field, but a field with a single widget is normally *merged* with it into one
  dictionary;
* the "checked" appearance state of a button lives on the widget, not on the
  field, and is an arbitrary name.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable
from typing import Any, cast

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

from pdffill.model import (
    FF_PUSHBUTTON,
    FF_RADIO,
    OFF_STATE,
    FieldKind,
    FormField,
    FormInfo,
    Widget,
)
from pdffill.xfa import detect_xfa

logger = logging.getLogger(__name__)

#: Entries a child field inherits from its ancestors when it does not set them.
INHERITABLE = ("/FT", "/Ff", "/V", "/DV", "/Opt", "/MaxLen", "/DA", "/Q")

#: ``/F`` annotation flag bits we care about (PDF 32000-1 table 165).
ANNOT_HIDDEN = 1 << 1
ANNOT_NOVIEW = 1 << 5

PdfSource = "str | Path | bytes | PdfReader | PdfWriter"


def open_pdf(source: Any) -> PdfReader | PdfWriter:
    """Return a ``pypdf`` document for *source*, decrypting with an empty password if needed."""
    if isinstance(source, (PdfReader, PdfWriter)):
        return source
    if isinstance(source, (bytes, bytearray)):
        reader = PdfReader(io.BytesIO(bytes(source)))
    else:
        reader = PdfReader(str(source))
    if reader.is_encrypted:
        # Many public forms are "encrypted" only to set permission bits and open
        # fine with an empty user password.
        reader.decrypt("")
    return reader


def open_reader(source: Any) -> PdfReader:
    """Like :func:`open_pdf`, but always a reader.

    A :class:`~pypdf.PdfWriter` is serialised and re-read, which is what makes
    it possible to chain fills: the output of one is a valid input to the next.
    """
    doc = open_pdf(source)
    if isinstance(doc, PdfWriter):
        buffer = io.BytesIO()
        doc.write(buffer)
        buffer.seek(0)
        return PdfReader(buffer)
    return doc


def _catalog(doc: PdfReader | PdfWriter) -> DictionaryObject:
    if isinstance(doc, PdfWriter):
        return doc.root_object
    return cast(DictionaryObject, doc.trailer["/Root"].get_object())


def get_acroform(doc: PdfReader | PdfWriter) -> DictionaryObject | None:
    """Return the ``/AcroForm`` dictionary of a document, or ``None``."""
    acro = _catalog(doc).get("/AcroForm")
    if acro is None:
        return None
    acro = acro.get_object()
    return acro if isinstance(acro, DictionaryObject) else None


_acroform = get_acroform


def _name(value: Any) -> str | None:
    """Normalise a PDF name to a plain string without the leading slash."""
    if value is None:
        return None
    text = str(value.get_object() if isinstance(value, IndirectObject) else value)
    return text[1:] if text.startswith("/") else text


def _widget_states(widget: DictionaryObject) -> tuple[str, ...]:
    """The appearance states declared by a widget's ``/AP /N`` entry.

    Returns an empty tuple when ``/N`` is a plain appearance *stream* rather than
    a state dictionary, which is the normal case for text fields. Streams are
    dictionaries too in ``pypdf``, so checking the type is what stops a text
    field from reporting ``BBox`` and ``Resources`` as its "states".
    """
    ap = widget.get("/AP")
    if ap is None:
        return ()
    ap = ap.get_object()
    if not isinstance(ap, DictionaryObject):
        return ()
    normal = ap.get("/N")
    if normal is None:
        return ()
    normal = normal.get_object()
    if isinstance(normal, StreamObject) or not isinstance(normal, DictionaryObject):
        return ()
    return tuple(sorted(str(k)[1:] if str(k).startswith("/") else str(k) for k in normal))


def _rect(widget: DictionaryObject) -> tuple[float, float, float, float] | None:
    raw = widget.get("/Rect")
    if raw is None:
        return None
    raw = raw.get_object()
    try:
        x0, y0, x1, y1 = (float(v.get_object()) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _page_index_map(doc: PdfReader | PdfWriter) -> dict[int, int]:
    """Map the object number of every widget annotation to its page index."""
    mapping: dict[int, int] = {}
    for page_index, page in enumerate(doc.pages):
        annots = page.get("/Annots")
        if annots is None:
            continue
        annots = annots.get_object()
        if not isinstance(annots, (ArrayObject, list)):
            continue
        for ref in annots:
            if isinstance(ref, IndirectObject):
                mapping.setdefault(ref.idnum, page_index)
            else:
                mapping.setdefault(id(ref), page_index)
    return mapping


def _resolve_kind(field_type: str | None, flags: int) -> FieldKind:
    """Turn ``/FT`` plus the ``/Ff`` bits into a usable field kind."""
    if field_type == "Btn":
        if flags & FF_PUSHBUTTON:
            return FieldKind.PUSHBUTTON
        if flags & FF_RADIO:
            return FieldKind.RADIO
        return FieldKind.CHECKBOX
    if field_type == "Tx":
        return FieldKind.TEXT
    if field_type == "Ch":
        from pdffill.model import FF_COMBO

        return FieldKind.DROPDOWN if flags & FF_COMBO else FieldKind.LISTBOX
    if field_type == "Sig":
        return FieldKind.SIGNATURE
    return FieldKind.UNKNOWN


def _read_value(raw: Any) -> str | list[str] | None:
    """Normalise ``/V`` or ``/DV`` into a string, a list of strings, or ``None``."""
    if raw is None:
        return None
    raw = raw.get_object() if isinstance(raw, IndirectObject) else raw
    if isinstance(raw, (ArrayObject, list)):
        return [v for v in (_read_value(item) for item in raw) if isinstance(v, str)]
    text = str(raw)
    if text.startswith("/"):
        return text[1:]
    return text


def _read_options(raw: Any) -> tuple[list[str], dict[str, str]]:
    """Parse a ``/Opt`` array into export values and their display labels."""
    if raw is None:
        return [], {}
    raw = raw.get_object()
    if not isinstance(raw, (ArrayObject, list)):
        return [], {}
    options: list[str] = []
    labels: dict[str, str] = {}
    for entry in raw:
        entry = entry.get_object()
        if isinstance(entry, (ArrayObject, list)) and len(entry) >= 2:
            export = str(entry[0].get_object())
            display = str(entry[1].get_object())
            options.append(export)
            if display and display != export:
                labels[export] = display
        else:
            options.append(str(entry))
    return options, labels


def _is_visible(widget: DictionaryObject) -> bool:
    raw = widget.get("/F")
    if raw is None:
        return True
    try:
        flags = int(raw.get_object())
    except (TypeError, ValueError):
        return True
    return not (flags & (ANNOT_HIDDEN | ANNOT_NOVIEW))


def _collect_widgets(
    node: DictionaryObject,
    widget_refs: Iterable[Any],
    pages: dict[int, int],
) -> list[Widget]:
    widgets: list[Widget] = []
    for ref in widget_refs:
        obj = ref.get_object() if isinstance(ref, IndirectObject) else ref
        if not isinstance(obj, DictionaryObject):
            continue
        key = ref.idnum if isinstance(ref, IndirectObject) else id(obj)
        states = _widget_states(obj)
        on_states = [s for s in states if s != OFF_STATE]
        widgets.append(
            Widget(
                page=pages.get(key, -1),
                rect=_rect(obj),
                on_state=on_states[0] if on_states else None,
                states=states,
                visible=_is_visible(obj),
            )
        )
    return widgets


def _walk(
    ref: Any,
    prefix: str,
    inherited: dict[str, Any],
    pages: dict[int, int],
    out: list[FormField],
    seen: set[int],
    objects: dict[str, tuple[DictionaryObject, list[DictionaryObject]]] | None = None,
) -> None:
    node = ref.get_object() if isinstance(ref, IndirectObject) else ref
    if not isinstance(node, DictionaryObject):
        return
    key = ref.idnum if isinstance(ref, IndirectObject) else id(node)
    if key in seen:
        logger.warning("Cycle in the AcroForm field tree at %r, skipping", prefix)
        return
    seen = seen | {key}

    partial = node.get("/T")
    partial = str(partial.get_object()) if partial is not None else None
    if partial and prefix:
        name = f"{prefix}.{partial}"
    else:
        name = partial or prefix

    attrs = dict(inherited)
    for entry in INHERITABLE:
        if entry in node:
            attrs[entry] = node[entry]

    kids_raw = node.get("/Kids")
    kids = list(kids_raw.get_object()) if kids_raw is not None else []
    child_fields = []
    widget_kids = []
    for kid in kids:
        kid_obj = kid.get_object() if isinstance(kid, IndirectObject) else kid
        if isinstance(kid_obj, DictionaryObject) and "/T" in kid_obj:
            child_fields.append(kid)
        else:
            widget_kids.append(kid)

    for kid in child_fields:
        _walk(kid, name, attrs, pages, out, seen, objects)

    if child_fields and not widget_kids:
        return  # purely an intermediate node

    # Terminal field. Its widgets are either the kids without /T, or - for the
    # common single-widget case - the field dictionary itself.
    if widget_kids:
        widget_refs: list[Any] = widget_kids
    elif node.get("/Subtype") == "/Widget" or "/Rect" in node:
        widget_refs = [ref]
    else:
        widget_refs = []

    try:
        flags = (
            int(attrs.get("/Ff", 0).get_object())
            if hasattr(attrs.get("/Ff", 0), "get_object")
            else int(attrs.get("/Ff", 0))
        )
    except (TypeError, ValueError):
        flags = 0
    kind = _resolve_kind(_name(attrs.get("/FT")), flags)
    if kind is FieldKind.UNKNOWN and not name:
        return

    options, labels = _read_options(attrs.get("/Opt"))
    max_len = attrs.get("/MaxLen")
    tooltip = node.get("/TU")

    widgets = _collect_widgets(node, widget_refs, pages)

    # For radio groups and check boxes, /Opt (when present) renames the
    # appearance states positionally, so it supplies option labels rather than
    # option values.
    if kind in (FieldKind.RADIO, FieldKind.CHECKBOX) and options:
        on_states = [w.on_state for w in widgets]
        labels = {
            state: display
            for state, display in zip(on_states, options, strict=False)
            if state and display and state != display
        }
        options = []

    field_name = name or f"<unnamed:{key}>"
    out.append(
        FormField(
            name=field_name,
            kind=kind,
            flags=flags,
            widgets=widgets,
            value=_read_value(attrs.get("/V")),
            default_value=_read_value(attrs.get("/DV")),
            options=options,
            option_labels=labels,
            max_length=int(max_len.get_object()) if max_len is not None else None,
            tooltip=str(tooltip.get_object()) if tooltip is not None else None,
        )
    )
    if objects is not None:
        widget_dicts = [
            obj
            for obj in (r.get_object() if isinstance(r, IndirectObject) else r for r in widget_refs)
            if isinstance(obj, DictionaryObject)
        ]
        if field_name in objects:
            objects[field_name][1].extend(widget_dicts)
        else:
            objects[field_name] = (node, widget_dicts)


def extract_form(source: Any, *, infer_labels: bool = False) -> FormInfo:
    """Extract the complete field inventory of *source*.

    Args:
        source: A path, raw PDF bytes, or an open ``PdfReader`` / ``PdfWriter``.
        infer_labels: Also guess a human-readable label for every field from the
            text printed next to its widget. Costs a text-extraction pass over
            every page that carries a widget.

    Returns:
        A :class:`~pdffill.model.FormInfo`. Fields come back in document order.
        A PDF without an AcroForm yields an empty inventory rather than an error,
        so that callers can tell "not a form" apart from "broken file".
    """
    return extract_form_and_objects(source, infer_labels=infer_labels)[0]


def extract_form_and_objects(
    source: Any, *, infer_labels: bool = False
) -> tuple[FormInfo, dict[str, tuple[DictionaryObject, list[DictionaryObject]]]]:
    """Like :func:`extract_form`, but also return the underlying PDF objects.

    The second element maps a fully qualified field name to its field dictionary
    and the list of its widget annotations. Writers need it because a value goes
    on the field (``/V``) while the appearance state goes on every widget
    (``/AS``).
    """
    doc = open_pdf(source)
    acro = _acroform(doc)
    info = FormInfo(
        fields=[],
        xfa=detect_xfa(acro),
        n_pages=len(doc.pages),
        title=_document_title(doc),
        need_appearances=bool(acro.get("/NeedAppearances", False)) if acro else False,
    )
    objects: dict[str, tuple[DictionaryObject, list[DictionaryObject]]] = {}
    if acro is None:
        return info, objects

    pages = _page_index_map(doc)
    fields_raw = acro.get("/Fields")
    roots = list(fields_raw.get_object()) if fields_raw is not None else []
    for ref in roots:
        _walk(ref, "", {}, pages, info.fields, set(), objects)

    _deduplicate(info.fields)
    if infer_labels:
        from pdffill.labels import infer_labels as _infer

        _infer(doc, info)
    return info, objects


def _deduplicate(fields: list[FormField]) -> None:
    """Merge fields that share a fully qualified name.

    Some generators list the same field twice in ``/Fields`` (once per widget).
    Keep the first occurrence and fold the extra widgets into it, so that the
    derived schema has one property per name.
    """
    index: dict[str, FormField] = {}
    keep: list[FormField] = []
    for field in fields:
        existing = index.get(field.name)
        if existing is None:
            index[field.name] = field
            keep.append(field)
            continue
        existing.widgets.extend(field.widgets)
        if existing.value is None:
            existing.value = field.value
    if len(keep) != len(fields):
        fields[:] = keep


def _document_title(doc: PdfReader | PdfWriter) -> str | None:
    try:
        meta = doc.metadata
    except Exception:  # pragma: no cover - broken metadata should never be fatal
        return None
    if not meta:
        return None
    title = meta.get("/Title")
    return str(title) if title else None
