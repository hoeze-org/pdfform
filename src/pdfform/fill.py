"""Write values into a PDF form.

Two details decide whether a fill actually shows up in a viewer:

* A button's value is a *name*, and the name is whatever appears in the widget's
  ``/AP /N`` dictionary. It is very often not ``Yes``.
* ``/V`` holds the logical value, ``/AS`` selects the appearance stream that gets
  drawn. Setting only ``/V`` produces a file whose value is correct and whose
  check box looks empty in every viewer.

Text fields need an appearance stream too. ``pypdf`` can generate one, and
``/NeedAppearances`` asks the viewer to regenerate them itself; by default both
are used, since neither is reliable alone across Acrobat, Preview, Chrome and
printers.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

from pdfform.extract import extract_form_and_objects, open_reader
from pdfform.flatten import flatten_widgets
from pdfform.model import (
    OFF_STATE,
    DynamicXfaError,
    FieldKind,
    FieldValueError,
    FormField,
    FormInfo,
    UnknownFieldError,
    XfaKind,
)
from pdfform.xfa import strip_xfa as _strip_xfa

logger = logging.getLogger(__name__)

TRUE_WORDS = {"true", "yes", "y", "on", "x", "1", "ja", "checked", "wahr"}
FALSE_WORDS = {"false", "no", "n", "off", "0", "nein", "unchecked", "falsch", ""}


def flatten_values(values: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping into dot-separated keys.

    Accepts the output of ``pdfform schema --nested`` as readily as the flat
    form. Lists are values, not structure, so they are left alone.
    """
    out: dict[str, Any] = {}
    if not isinstance(values, dict):
        raise FieldValueError(f"Expected an object of field values, got {type(values).__name__}")
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten_values(value, name))
        else:
            out[name] = value
    return out


def _match_field(name: str, info: FormInfo, index: dict[str, FormField]) -> FormField:
    field = index.get(name)
    if field is not None:
        return field
    # Allow addressing a nested field by its partial name when it is unambiguous.
    candidates = [f for f in info.fields if f.name.rsplit(".", 1)[-1] == name]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise UnknownFieldError(
            f"Field name {name!r} is ambiguous, it matches: " + ", ".join(sorted(c.name for c in candidates))
        )
    raise UnknownFieldError(f"The form has no field named {name!r}")


def _signed_fields(info: FormInfo) -> list[str]:
    """Names of the signature fields that already hold a signature."""
    return [f.name for f in info.fields if f.kind is FieldKind.SIGNATURE and f.value is not None]


def _as_text(value: Any, field: FormField, strict: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "Yes" if value else "No"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise FieldValueError(f"{field.name}: cannot write {type(value).__name__} into a text field")
    if field.max_length and len(text) > field.max_length:
        message = f"{field.name}: value is {len(text)} characters but the field allows {field.max_length}"
        if strict:
            raise FieldValueError(message)
        logger.warning("%s; writing it anyway", message)
    return text


def _as_checkbox(value: Any, field: FormField) -> str | None:
    """Return the appearance state to set, or ``None`` for unchecked."""
    on_state = field.on_states[0] if field.on_states else "Yes"
    if value is None:
        return None
    if isinstance(value, bool):
        return on_state if value else None
    text = str(value).strip()
    lowered = text.lower()
    if text in field.on_states or lowered in {s.lower() for s in field.on_states}:
        return next(s for s in field.on_states if s.lower() == lowered)
    if lowered in TRUE_WORDS:
        return on_state
    if lowered in FALSE_WORDS or lowered == OFF_STATE.lower():
        return None
    raise FieldValueError(
        f"{field.name}: {value!r} is not a check box value. Use true/false, or the on-state {on_state!r}."
    )


def _as_radio(value: Any, field: FormField) -> str | None:
    if value is None or value is False or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if text == OFF_STATE:
        return None
    states = field.on_states
    if text in states:
        return text
    lowered = text.lower()
    for state in states:
        if state.lower() == lowered:
            return state
    for state, label in field.option_labels.items():
        if label.lower() == lowered and state in states:
            return state
    raise FieldValueError(
        f"{field.name}: {value!r} is not one of the options. Available: " + (", ".join(states) or "(none)")
    )


def _as_choice(value: Any, field: FormField, strict: bool) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not field.multi_select and len(value) > 1:
            raise FieldValueError(f"{field.name}: field accepts a single value, got {len(value)}")
        return [v for v in (_as_choice(item, field, strict) for item in value) if isinstance(v, str)]

    text = str(value)
    if not field.options or field.editable_choice:
        return text
    if text in field.options:
        return text
    lowered = text.lower()
    for option in field.options:
        if option.lower() == lowered:
            return option
    for export, label in field.option_labels.items():
        if label.lower() == lowered:
            return export
    message = f"{field.name}: {text!r} is not one of the options. Available: " + ", ".join(field.options)
    if strict:
        raise FieldValueError(message)
    logger.warning("%s; writing it anyway", message)
    return text


def _set_button(field_obj: DictionaryObject, widgets: list[DictionaryObject], state: str | None) -> None:
    """Set ``/V`` on the field and ``/AS`` on every one of its widgets.

    A radio group distinguishes its options by giving each widget a different
    on-state, so exactly the widget whose ``/AP /N`` holds *state* is switched on
    and all the others go to ``/Off``.
    """
    target = NameObject(f"/{state}") if state else NameObject(f"/{OFF_STATE}")
    field_obj[NameObject("/V")] = target
    for widget in widgets:
        appearance = widget.get("/AP")
        states: set[str] = set()
        if appearance is not None:
            normal = appearance.get_object().get("/N")
            if normal is not None and hasattr(normal.get_object(), "keys"):
                states = {str(k) for k in normal.get_object()}
        widget[NameObject("/AS")] = target if (state and str(target) in states) else NameObject(f"/{OFF_STATE}")


def _set_value(field_obj: DictionaryObject, value: str | list[str] | None) -> None:
    if value is None:
        field_obj.pop(NameObject("/V"), None)
    elif isinstance(value, list):
        field_obj[NameObject("/V")] = ArrayObject([TextStringObject(v) for v in value])
    else:
        field_obj[NameObject("/V")] = TextStringObject(value)
    # /I caches the selected indices of a choice field and goes stale on write.
    field_obj.pop(NameObject("/I"), None)


def fill_form(
    source: Any,
    values: dict[str, Any],
    output: Any = None,
    *,
    flatten: bool = False,
    need_appearances: bool = True,
    strict: bool = True,
    strip_xfa: bool | None = None,
    stamp: dict[str, Any] | None = None,
    stamp_fit: str = "contain",
) -> bytes:
    """Fill a PDF form and return the resulting document as bytes.

    Args:
        source: Path, bytes, or an open ``PdfReader`` of the blank form.
        values: Field values, keyed by fully qualified field name. Nested
            mappings are flattened onto dot-separated names. A ``None`` value
            clears the field; a missing key leaves it untouched.
        output: Optional path or writable binary stream. The bytes are returned
            either way.
        flatten: Bake the field appearances into the page content and drop the
            form, producing a non-editable document.
        need_appearances: Set the ``/NeedAppearances`` flag so viewers rebuild
            the appearance streams themselves. Ignored when flattening.
        strict: Reject unknown field names and values that do not fit their
            field. When false, both are logged and skipped or forced.
        strip_xfa: Remove the XFA layer. The default removes it for static XFA
            forms, where leaving it in place lets Acrobat overwrite the values
            that were just written.
        stamp: Signature images to draw, keyed by signature field name, each a
            path or bytes as for :func:`~pdfform.stamp.stamp_signature`. Applied
            after the values and before flattening, so a flattened document
            carries the image on the page.
        stamp_fit: ``contain`` or ``stretch``, for every stamp.

    Filling a signed document breaks its signature. That is logged as a warning
    and not refused, because certified blank templates are signed too.

    Raises:
        DynamicXfaError: The form is dynamic XFA and cannot be filled this way.
        UnknownFieldError: A key does not name a field (strict mode).
        FieldValueError: A value does not fit its field (strict mode).
        SignedDocumentError: *stamp* is given and the document is already signed.
    """
    writer = PdfWriter(clone_from=open_reader(source))
    info, objects = extract_form_and_objects(writer)

    if info.xfa is XfaKind.DYNAMIC:
        raise DynamicXfaError(
            "This is a dynamic XFA form. Its fields are generated by script at open time, "
            "so writing AcroForm values has no effect."
        )
    signed = _signed_fields(info)
    if signed and not stamp:  # a stamp refuses a signed document further down
        # Not refused: certified blank templates are signed, and filling them is common.
        logger.warning("The document is signed (%s). Filling rewrites it and breaks the signature", ", ".join(signed))
    if strip_xfa is None:
        strip_xfa = info.xfa is XfaKind.HYBRID
    if strip_xfa and _strip_xfa(writer.root_object):
        logger.info("Removed the XFA layer so the AcroForm values are authoritative")

    index = {f.name: f for f in info.fields}
    text_updates: dict[str, Any] = {}
    applied = 0

    for name, raw in flatten_values(values).items():
        try:
            field = _match_field(name, info, index)
        except UnknownFieldError:
            if strict:
                raise
            logger.warning("Skipping unknown field %r", name)
            continue

        if not field.kind.holds_value:
            message = f"{field.name}: {field.kind.value} fields hold no value"
            if strict:
                raise FieldValueError(message)
            logger.warning("%s; skipping", message)
            continue
        if field.kind is FieldKind.SIGNATURE:
            message = f"{field.name}: signature fields cannot be filled with a value, use stamp_signature or sign_pdf"
            if strict:
                raise FieldValueError(message)
            logger.warning("%s; skipping", message)
            continue
        if field.read_only:
            logger.warning("%s is read-only; writing to it anyway", field.name)

        entry = objects.get(field.name)
        if entry is None:  # pragma: no cover - extraction always records objects
            raise UnknownFieldError(f"Could not locate the object for field {field.name!r}")
        field_obj, widget_objs = entry

        if field.kind is FieldKind.CHECKBOX:
            _set_button(field_obj, widget_objs, _as_checkbox(raw, field))
        elif field.kind is FieldKind.RADIO:
            _set_button(field_obj, widget_objs, _as_radio(raw, field))
        elif field.kind is FieldKind.TEXT:
            text = _as_text(raw, field, strict)
            _set_value(field_obj, text or None)
            text_updates[field.name] = text
        else:
            choice = _as_choice(raw, field, strict)
            _set_value(field_obj, choice)
            text_updates[field.name] = choice if choice is not None else ""
        applied += 1

    if text_updates:
        # pypdf builds the appearance stream for text and choice fields; /V has
        # already been written above so the value survives even when a widget is
        # not reachable from the page tree.
        try:
            writer.update_page_form_field_values(None, text_updates, auto_regenerate=False)
        except Exception as exc:  # pragma: no cover - depends on the source file
            logger.warning("Could not regenerate text appearances (%s); relying on /NeedAppearances", exc)
            need_appearances = True

    if stamp:
        from pdfform.stamp import apply_stamp

        for field_name, image in stamp.items():
            apply_stamp(writer, info, objects, field_name, image, fit=stamp_fit)  # type: ignore[arg-type]

    if flatten:
        flatten_widgets(writer)
    else:
        writer.set_need_appearances_writer(need_appearances)

    logger.info("Filled %d field(s)", applied)
    return _write_out(writer, output)


def _write_out(writer: PdfWriter, output: Any) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    if output is None:
        return data
    if isinstance(output, (str, Path)):
        Path(output).write_bytes(data)
    else:
        output.write(data)
    return data


def strip_xfa_layer(source: Any, output: Any = None) -> bytes:
    """Drop the XFA layer of a document, leaving the AcroForm intact.

    The ``pdftk ... drop_xfa`` equivalent, without needing ``pdftk``.
    """
    writer = PdfWriter(clone_from=open_reader(source))
    if not _strip_xfa(writer.root_object):
        logger.info("No XFA layer present")
    return _write_out(writer, output)
