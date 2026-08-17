"""Guess human-readable labels for form fields from the surrounding page text.

Field names in real forms are frequently useless (``Text12``, ``Gruppe3``,
``Kontrollkästchen1``). A schema derived from names alone is structurally
correct but tells a human - or a model - nothing about what to write where.

The document's own ``/TU`` tooltip is the better source when it exists, so this
module is only a fallback. It extracts every text run with its position, then
picks the nearest run in the direction a label conventionally sits: to the left
of, or above, a text box; to the right of a check box or radio button.

This is a heuristic over page geometry. It is right most of the time and wrong
some of the time, which is why it is opt-in and why the result is reported
separately from the tooltip.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from pdffill.model import FieldKind, FormField, FormInfo, Widget

logger = logging.getLogger(__name__)

#: Vertical tolerance, as a fraction of widget height, for "on the same line".
_LINE_SLACK = 0.6
#: How far above a widget a caption may sit, in points.
_ABOVE_LIMIT = 26.0
#: How far to the side a caption may sit, in points.
_SIDE_LIMIT = 320.0
#: Rough width of one character as a fraction of the font size.
_CHAR_WIDTH = 0.5

_WHITESPACE = re.compile(r"\s+")
#: Leaders and separators that carry no meaning in a caption.
_TRIM = " \t\r\n:.…_-–—"
#: Box and bullet glyphs. Forms that were built by dropping widgets onto a
#: printed layout have these sitting right next to every check box, where they
#: would otherwise win as the nearest text.
_GLYPHS = "☐☒☑□■▪▫✓✔✗✘●○◯•·*x"


@dataclass
class _Run:
    text: str
    x: float
    y: float
    size: float

    @property
    def x_end(self) -> float:
        return self.x + len(self.text) * self.size * _CHAR_WIDTH


def _page_runs(page) -> list[_Run]:
    """Collect the positioned text runs of a page."""
    runs: list[_Run] = []

    def visitor(text, cm, tm, font_dict, font_size):  # noqa: ANN001 - pypdf callback
        cleaned = _WHITESPACE.sub(" ", text).strip()
        if not cleaned:
            return
        try:
            x, y = float(tm[4]), float(tm[5])
            size = abs(float(font_size)) if font_size else abs(float(tm[3])) or 10.0
        except (TypeError, ValueError, IndexError):
            return
        runs.append(_Run(cleaned, x, y, size or 10.0))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception as exc:  # pragma: no cover - extraction is best effort
        logger.debug("Text extraction failed on a page: %s", exc)
        return []
    return runs


def _clean(text: str) -> str:
    """Normalise a candidate caption, rejecting the ones that say nothing."""
    cleaned = text.strip(_TRIM + _GLYPHS).strip(_TRIM)
    return cleaned if any(char.isalnum() for char in cleaned) else ""


def _label_for(widget: Widget, runs: list[_Run], prefer_right: bool) -> str | None:
    if widget.rect is None:
        return None
    x0, y0, x1, y1 = widget.rect
    height = max(y1 - y0, 1.0)
    slack = height * _LINE_SLACK
    mid_y = (y0 + y1) / 2

    same_line = [r for r in runs if y0 - slack <= r.y <= y1 + slack]
    right = sorted((r for r in same_line if r.x >= x1 - 1), key=lambda r: r.x - x1)
    left = sorted((r for r in same_line if r.x_end <= x0 + 1), key=lambda r: x0 - r.x_end)

    order = (right, left) if prefer_right else (left, right)
    for candidates in order:
        for run in candidates:
            gap = (run.x - x1) if candidates is right else (x0 - run.x_end)
            if gap > _SIDE_LIMIT:
                break
            text = _clean(run.text)
            if text:
                return text

    above = [r for r in runs if y1 <= r.y <= y1 + _ABOVE_LIMIT and r.x_end >= x0 - 8 and r.x <= x1 + 8]
    above.sort(key=lambda r: (r.y - y1, abs(r.x - x0)))
    for run in above:
        text = _clean(run.text)
        if text:
            return text

    below = [r for r in runs if mid_y - _ABOVE_LIMIT <= r.y <= y0 and r.x_end >= x0 - 8 and r.x <= x1 + 8]
    below.sort(key=lambda r: (y0 - r.y, abs(r.x - x0)))
    for run in below:
        text = _clean(run.text)
        if text:
            return text
    return None


def infer_labels(doc, info: FormInfo) -> None:
    """Fill in ``label`` on every widget and field of *info*, in place.

    Only pages that actually carry a widget are read.
    """
    wanted = {page for field in info.fields for page in field.pages}
    runs_by_page: dict[int, list[_Run]] = {}
    for page_index in sorted(wanted):
        if 0 <= page_index < len(doc.pages):
            runs_by_page[page_index] = _page_runs(doc.pages[page_index])

    for field in info.fields:
        prefer_right = field.kind in (FieldKind.CHECKBOX, FieldKind.RADIO)
        for widget in field.widgets:
            runs = runs_by_page.get(widget.page)
            if runs:
                widget.label = _label_for(widget, runs, prefer_right)
        _apply_field_label(field)


def _apply_field_label(field: FormField) -> None:
    labels = [w.label for w in field.widgets if w.label]
    if not labels:
        return
    if field.kind is not FieldKind.RADIO:
        field.label = labels[0]
        return

    # Each widget of a radio group is one option, so its neighbouring text names
    # that option rather than the group. When two options end up with the same
    # caption the guess is provably wrong - one text run sits nearest to both -
    # so it is discarded rather than reported.
    counts = Counter(labels)
    for widget in field.widgets:
        if widget.on_state and widget.label and counts[widget.label] == 1:
            field.option_labels.setdefault(widget.on_state, widget.label)
