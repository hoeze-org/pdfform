"""Derive a JSON Schema from a PDF form.

The mapping is deliberately boring, because a boring schema is what validators
and language models handle well:

===============  ===========================================================
Field kind       JSON Schema
===============  ===========================================================
text             ``{"type": "string"}``, plus ``maxLength`` when ``/MaxLen``
check box        ``{"type": "boolean"}``
radio group      ``{"type": "string", "enum": [...on states...]}``
dropdown         ``{"type": "string", "enum": [...]}``, or free string when
                 the combo box is editable (``/Ff`` bit 19)
list box         same as dropdown; an array when multi-select is set
push button      omitted, it holds no value
signature        omitted by default, it cannot be filled with a value
===============  ===========================================================

Everything needed to write the value back - the field kind, its on-state, the
pages it appears on - is preserved under the ``x-pdf`` keyword, which validators
ignore.
"""

from __future__ import annotations

from typing import Any

from pdffill.model import (
    OFF_STATE,
    DynamicXfaError,
    FieldKind,
    FormField,
    FormInfo,
    PdfFormError,
    XfaKind,
)

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

#: Keyword used for the PDF-specific annotations attached to each property.
PDF_KEYWORD = "x-pdf"


def field_schema(field: FormField) -> dict[str, Any]:
    """Build the JSON Schema fragment for a single field."""
    schema: dict[str, Any]

    if field.kind is FieldKind.TEXT:
        schema = {"type": "string"}
        if field.max_length:
            schema["maxLength"] = field.max_length
    elif field.kind is FieldKind.CHECKBOX:
        schema = {"type": "boolean"}
    elif field.kind is FieldKind.RADIO:
        schema = {"type": "string"}
        states = field.on_states
        if states:
            schema["enum"] = states
    elif field.kind in (FieldKind.DROPDOWN, FieldKind.LISTBOX):
        item: dict[str, Any] = {"type": "string"}
        if field.options and not field.editable_choice:
            item["enum"] = list(field.options)
        elif field.options:
            item["examples"] = list(field.options)
        if field.multi_select:
            schema = {"type": "array", "items": item, "uniqueItems": True}
        else:
            schema = item
    elif field.kind is FieldKind.SIGNATURE:
        schema = {"type": "string"}
    else:
        schema = {}

    description = _description(field)
    if description:
        schema["description"] = description
    if field.default_value not in (None, "", []):
        schema["default"] = _coerce_default(field)
    if field.read_only:
        schema["readOnly"] = True

    schema[PDF_KEYWORD] = _pdf_annotation(field)
    return schema


def _description(field: FormField) -> str | None:
    parts: list[str] = []
    primary = field.tooltip or field.label
    if primary and primary != field.name:
        parts.append(primary)
    if field.option_labels:
        rendered = ", ".join(f"{value} = {label}" for value, label in field.option_labels.items())
        parts.append(f"Options: {rendered}")
    if field.multiline:
        parts.append("Multi-line.")
    if field.comb:
        parts.append(f"Comb field of {field.max_length} characters.")
    return " ".join(parts) or None


def _coerce_default(field: FormField) -> Any:
    if field.kind is FieldKind.CHECKBOX:
        return field.default_value not in (None, "", OFF_STATE)
    return field.default_value


def _pdf_annotation(field: FormField) -> dict[str, Any]:
    annotation: dict[str, Any] = {
        "field": field.name,
        "kind": field.kind.value,
        "pages": field.pages,
    }
    if field.kind is FieldKind.CHECKBOX:
        annotation["onState"] = field.on_states[0] if field.on_states else "Yes"
        annotation["offState"] = OFF_STATE
    if field.kind is FieldKind.RADIO and field.option_labels:
        annotation["optionLabels"] = dict(field.option_labels)
    if field.read_only:
        annotation["readOnly"] = True
    if field.required:
        annotation["required"] = True
    if field.flags:
        annotation["flags"] = field.flags
    return annotation


def _nest(flat: dict[str, Any], required: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Turn dot-separated field names into nested objects.

    Fully qualified AcroForm names are built by joining the ``/T`` of every
    ancestor with ``.``, so splitting on ``.`` normally recovers the intended
    grouping. A partial name may itself contain a literal dot, in which case the
    grouping is wrong but no information is lost: ``x-pdf.field`` still holds the
    real, flat name.
    """
    root: dict[str, Any] = {}
    nested_required: list[str] = []

    for name, schema in flat.items():
        parts = name.split(".")
        node = root
        ok = True
        for part in parts[:-1]:
            child = node.setdefault(part, {"type": "object", "properties": {}})
            if "properties" not in child:
                # A leaf already occupies this slot; keep the flat name instead
                # of destroying it.
                ok = False
                break
            node = child["properties"]
        if not ok or parts[-1] in node:
            root[name] = schema
            continue
        node[parts[-1]] = schema

    for name in required:
        if "." not in name:
            nested_required.append(name)
    return root, nested_required


def build_schema(
    info: FormInfo,
    *,
    nested: bool = False,
    include_read_only: bool = False,
    include_signatures: bool = False,
    title: str | None = None,
) -> dict[str, Any]:
    """Build a JSON Schema document describing the fillable fields of a form.

    Args:
        info: The extracted form.
        nested: Split dot-separated field names into nested objects.
        include_read_only: Also emit read-only fields. They are described but
            cannot be filled.
        include_signatures: Also emit signature fields. ``pdffill`` cannot sign,
            so they are left out unless asked for.
        title: Overrides the schema title.

    Raises:
        DynamicXfaError: If the form is dynamic XFA and has no static field list.
    """
    if info.xfa is XfaKind.DYNAMIC:
        raise DynamicXfaError(
            "This is a dynamic XFA form: the AcroForm is an empty stub and the real "
            "form is script-generated XML, so it has no fixed field list. Inspect the "
            "XFA template packet with `pdffill xfa --packet template` instead."
        )

    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in info.fields:
        if not field.kind.holds_value:
            continue
        if field.kind is FieldKind.SIGNATURE and not include_signatures:
            continue
        if field.read_only and not include_read_only:
            continue
        properties[field.name] = field_schema(field)
        if field.required and not field.read_only:
            required.append(field.name)

    if nested:
        properties, required = _nest(properties, required)

    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "title": title or info.title or "PDF form",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    if info.xfa is XfaKind.HYBRID:
        schema[PDF_KEYWORD] = {
            "xfa": "hybrid",
            "warning": (
                "This form carries a static XFA layer. Acrobat may prefer the XFA data "
                "over the AcroForm values on open; fill with --strip-xfa to remove it."
            ),
        }
    return schema


def current_values(
    info: FormInfo,
    *,
    include_empty: bool = False,
    include_read_only: bool = False,
) -> dict[str, Any]:
    """Return the values currently stored in the form, keyed by field name.

    The defaults match :func:`build_schema`, so the result validates against the
    derived schema. That is what makes ``pdffill values`` a usable starting point
    for ``pdffill fill``.
    """
    out: dict[str, Any] = {}
    for field in info.fields:
        if not field.kind.holds_value or field.kind is FieldKind.SIGNATURE:
            continue
        if field.read_only and not include_read_only:
            continue
        value: Any = field.value
        if field.kind is FieldKind.CHECKBOX:
            value = value not in (None, "", OFF_STATE)
            if not value and not include_empty:
                continue
        elif value in (None, "", []) or value == OFF_STATE:
            if not include_empty:
                continue
            value = _empty_for(field)
        out[field.name] = value
    return out


def validate_values(schema: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """Check *values* against a derived schema and return the problems found.

    Keys set to ``None`` are skipped: the schema describes what a filled field
    looks like, while ``None`` means "clear this field", which is a valid
    instruction rather than a value.

    Note that filling itself is deliberately more permissive than the schema.
    ``pdffill fill`` accepts ``"yes"`` for a check box, an option's display label
    instead of its export value, and an unambiguous partial field name. Use this
    when the data is meant to be schema-clean, not as a precondition for filling.

    Returns:
        Human-readable messages, most significant first. An empty list means the
        values validate.

    Raises:
        PdfFormError: If ``jsonschema`` is not installed.
    """
    try:
        import jsonschema
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
        raise PdfFormError("Validation needs jsonschema. Install it with: pip install 'pdffill[validate]'") from exc

    instance = {key: value for key, value in values.items() if value is not None}
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    return [f"{'.'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}" for error in errors]


def _empty_for(field: FormField) -> Any:
    """How an unset field is represented in a values template.

    A radio group or a dropdown with nothing selected becomes ``null`` rather
    than ``""``, because the empty string is not one of its options and would
    make the template fail its own schema. :func:`~pdffill.fill.fill_form`
    reads ``null`` as "clear this field".
    """
    if field.kind is FieldKind.CHECKBOX:
        return False
    if field.multi_select:
        return []
    if field.kind in (FieldKind.RADIO, FieldKind.DROPDOWN, FieldKind.LISTBOX):
        return None
    return ""
