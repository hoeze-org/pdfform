"""Data model describing the fields of an AcroForm.

The classes here are deliberately free of ``pypdf`` types: they are a plain,
serialisable snapshot of a form, which is what both the JSON Schema derivation
and the filler operate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Field flags (/Ff), PDF 32000-1:2008 table 221 ff. Bit numbers in the spec are
# 1-based, so bit N is ``1 << (N - 1)``.
FF_READ_ONLY = 1 << 0
FF_REQUIRED = 1 << 1
FF_NO_EXPORT = 1 << 2
# Text fields
FF_MULTILINE = 1 << 12
FF_PASSWORD = 1 << 13
FF_FILE_SELECT = 1 << 20
FF_DO_NOT_SPELL_CHECK = 1 << 22
FF_DO_NOT_SCROLL = 1 << 23
FF_COMB = 1 << 24
FF_RICH_TEXT = 1 << 25
# Button fields
FF_NO_TOGGLE_TO_OFF = 1 << 14
FF_RADIO = 1 << 15
FF_PUSHBUTTON = 1 << 16
FF_RADIOS_IN_UNISON = 1 << 25
# Choice fields
FF_COMBO = 1 << 17
FF_EDIT = 1 << 18
FF_SORT = 1 << 19
FF_MULTI_SELECT = 1 << 21
FF_COMMIT_ON_SEL_CHANGE = 1 << 26

#: The name of the "unchecked" appearance state, fixed by the PDF spec.
OFF_STATE = "Off"


class FieldKind(str, Enum):
    """The kind of form field, after resolving ``/FT`` together with ``/Ff``.

    ``/FT /Btn`` covers three unrelated widgets (push button, radio group and
    check box) that are only told apart by flag bits, so the raw field type is
    not useful on its own.
    """

    TEXT = "text"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    DROPDOWN = "dropdown"
    LISTBOX = "listbox"
    PUSHBUTTON = "pushbutton"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"

    @property
    def holds_value(self) -> bool:
        """Whether the field can carry a value that is worth filling in."""
        return self not in (FieldKind.PUSHBUTTON, FieldKind.UNKNOWN)


@dataclass
class Widget:
    """One on-page annotation belonging to a field.

    A field may own several widgets: radio groups have one per option, and a
    field repeated on every page (a signature line, say) has one per page.
    """

    page: int
    """Zero-based index of the page the widget sits on, or ``-1`` if the widget
    is not referenced by any page tree entry."""

    rect: tuple[float, float, float, float] | None
    """``(x0, y0, x1, y1)`` in PDF user space, normalised so ``x0 <= x1``."""

    on_state: str | None = None
    """For button widgets, the ``/AP /N`` key that is not ``/Off``. This is the
    value that has to be written to turn the widget on and is *not* reliably
    ``Yes``."""

    states: tuple[str, ...] = ()
    """All appearance states declared by the widget, ``Off`` included."""

    visible: bool = True
    """False when the annotation carries the hidden or no-view ``/F`` flag."""

    label: str | None = None
    """Text found next to this widget, when label inference ran."""


@dataclass
class FormField:
    """A single, fully qualified form field."""

    name: str
    """Fully qualified name, i.e. the ``/T`` entries of the field and all of its
    ancestors joined with ``.``."""

    kind: FieldKind
    flags: int = 0
    widgets: list[Widget] = field(default_factory=list)

    value: str | list[str] | None = None
    """Current value (``/V``), as a plain string, or a list for multi-select."""

    default_value: str | list[str] | None = None
    """Reset value (``/DV``)."""

    options: list[str] = field(default_factory=list)
    """Export values of a choice or radio field, in document order."""

    option_labels: dict[str, str] = field(default_factory=dict)
    """Export value to human-readable label, for ``/Opt`` arrays that use the
    ``[export, display]`` pair form."""

    max_length: int | None = None
    tooltip: str | None = None
    """The ``/TU`` alternate field name; usually the closest thing to a
    human-readable description that the document itself provides."""

    label: str | None = None
    """Label guessed from the text printed next to the widget. Only populated
    when label inference is requested."""

    @property
    def read_only(self) -> bool:
        return bool(self.flags & FF_READ_ONLY)

    @property
    def required(self) -> bool:
        return bool(self.flags & FF_REQUIRED)

    @property
    def multiline(self) -> bool:
        return bool(self.flags & FF_MULTILINE)

    @property
    def password(self) -> bool:
        return bool(self.flags & FF_PASSWORD)

    @property
    def comb(self) -> bool:
        return bool(self.flags & FF_COMB)

    @property
    def multi_select(self) -> bool:
        return bool(self.flags & FF_MULTI_SELECT)

    @property
    def editable_choice(self) -> bool:
        """A combo box that also accepts free text (``/Ff`` bit 19)."""
        return bool(self.flags & FF_EDIT)

    @property
    def on_states(self) -> list[str]:
        """The distinct on-states across all widgets of this field.

        For a check box this is normally a single entry; for a radio group it is
        one entry per option, in widget order.
        """
        seen: list[str] = []
        for widget in self.widgets:
            if widget.on_state is not None and widget.on_state not in seen:
                seen.append(widget.on_state)
        return seen

    @property
    def pages(self) -> list[int]:
        return sorted({w.page for w in self.widgets if w.page >= 0})

    def describe(self) -> str | None:
        """Best available human-readable description of the field."""
        return self.tooltip or self.label


class XfaKind(str, Enum):
    """How much of the form lives in an XFA packet rather than in the AcroForm."""

    NONE = "none"
    """Plain AcroForm. Everything works."""

    HYBRID = "hybrid"
    """Static XFA: real AcroForm fields exist alongside an ``/XFA`` packet.
    Readable and fillable, but Acrobat may prefer the XFA data on open, so the
    XFA layer should be dropped from the output."""

    DYNAMIC = "dynamic"
    """Dynamic XFA: the AcroForm side is an empty stub and the real form is
    script-driven XML. There is no fixed field list to derive a schema from."""


@dataclass
class FormInfo:
    """Everything extracted from one PDF."""

    fields: list[FormField]
    xfa: XfaKind = XfaKind.NONE
    n_pages: int = 0
    title: str | None = None
    need_appearances: bool = False

    def __iter__(self):
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    @property
    def fillable(self) -> list[FormField]:
        """Fields that can hold a value and are not read-only."""
        return [f for f in self.fields if f.kind.holds_value and not f.read_only]

    def by_name(self, name: str) -> FormField:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)


class PdfFormError(Exception):
    """Base class for every error raised by :mod:`pdfform`."""


class DynamicXfaError(PdfFormError):
    """Raised when a form is dynamic XFA and therefore has no static schema."""


class UnknownFieldError(PdfFormError):
    """Raised when a value is supplied for a field the form does not have."""


class FieldValueError(PdfFormError):
    """Raised when a value does not fit the field it is meant to go into."""
