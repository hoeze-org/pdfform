"""Decode PDF names that are not UTF-8.

A name object is a byte sequence without a declared encoding. ISO 32000 only
recommends UTF-8 for names that are read as text. Producers that ignore the
recommendation usually write the code page of the author's system, as in
``/R#fcckfahrt`` for "Rückfahrt" in Latin-1. Button on-states are such names,
and pdfform shows them to the user and matches values against them.

pypdf tries the encodings in ``NameObject.CHARSETS`` in order and keeps the
first one that decodes. Its default puts GBK before Latin-1, so a Latin-1 ``ü``
before an ASCII letter turns into a Chinese character. pdfform instead tries
UTF-8, then the Windows code page for the document's ``/Lang``, then Latin-1,
which decodes any byte. :func:`name_charsets` replaces that guess.

The guess does not break the buttons. pypdf writes every name in an object
dictionary back as UTF-8, so a button's ``/AS`` and the ``/AP`` key it selects
keep matching bytes. Names inside content streams keep their bytes.

pypdf also tries ``NameObject.CHARSETS`` for a byte string that is not UTF-16,
so the charsets apply to such strings too.
"""

from __future__ import annotations

import codecs
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pypdf.generic import NameObject

CODE_PAGES: dict[str, str] = {
    **dict.fromkeys(("bs", "cs", "hr", "hu", "pl", "ro", "sk", "sl", "sq", "sr-latn"), "cp1250"),
    **dict.fromkeys(
        ("az-cyrl", "be", "bg", "bs-cyrl", "kk", "ky", "mk", "mn", "ru", "sr", "tt", "uk", "uz-cyrl"), "cp1251"
    ),
    "el": "cp1253",
    **dict.fromkeys(("az", "tr", "uz"), "cp1254"),
    "he": "cp1255",
    **dict.fromkeys(("ar", "fa", "ur"), "cp1256"),
    **dict.fromkeys(("et", "lt", "lv"), "cp1257"),
    "vi": "cp1258",
    "th": "cp874",
    "ja": "cp932",
    "ko": "cp949",
    **dict.fromkeys(("zh", "zh-hans"), "gbk"),
    **dict.fromkeys(("zh-hant", "zh-hk", "zh-mo", "zh-tw"), "cp950"),
}
"""The Windows code page that legacy producers write for a language.

Keys are the primary subtag of ``/Lang`` in lower case, or the primary subtag
with a script or region subtag where one language uses several code pages.
"""

DEFAULT_CODE_PAGE = "cp1252"
"""The code page for every other language, and for a document without ``/Lang``."""

_chosen: ContextVar[tuple[str, ...] | None] = ContextVar("pdfform_name_charsets", default=None)

_lock = threading.RLock()
"""Held while ``NameObject.CHARSETS`` is changed. Reentrant, because pdfform calls nest."""


def code_page(lang: str | None) -> str:
    """Return the Windows code page for a ``/Lang`` tag such as ``de-DE`` or ``zh-Hant-TW``."""
    primary, *rest = (lang or "").strip().lower().replace("_", "-").split("-")
    for subtag in rest:
        page = CODE_PAGES.get(f"{primary}-{subtag}")
        if page is not None:
            return page
    return CODE_PAGES.get(primary, DEFAULT_CODE_PAGE)


def lang_charsets(lang: str | None) -> tuple[str, ...]:
    """Return the charsets pdfform tries for a document with this ``/Lang``."""
    return ("utf-8", code_page(lang), "latin1")


@contextmanager
def name_charsets(*charsets: str) -> Iterator[None]:
    """Decode names with *charsets*, tried in order, instead of guessing from ``/Lang``.

    This applies to every pdfform call in the ``with`` block. Calls in other
    threads keep their own charsets::

        with name_charsets("utf-8", "cp1250"):
            info = extract_form("form.pdf")

    Raises:
        ValueError: *charsets* is empty or names an unknown codec.
    """
    if not charsets:
        raise ValueError("Give at least one charset")
    for charset in charsets:
        try:
            codecs.lookup(charset)
        except LookupError:
            raise ValueError(f"Unknown charset {charset!r}") from None
    token = _chosen.set(tuple(charsets))
    try:
        yield
    finally:
        _chosen.reset(token)


@contextmanager
def decoding_names(lang: Callable[[], str | None]) -> Iterator[None]:
    """Set ``NameObject.CHARSETS`` for one pdfform call, and restore it afterwards.

    *lang* returns the document's ``/Lang``. It is only called when no
    :func:`name_charsets` block is active.

    pypdf reads an object when it is first used, so the block must span the
    whole call, not only the opening of the file. ``NameObject.CHARSETS`` is a
    class attribute, so the block holds a lock: pdfform calls in other threads
    wait until it ends. Other code that uses pypdf in another thread still sees
    the changed charsets while the block runs.
    """
    with _lock:
        charsets = _chosen.get() or lang_charsets(lang())
        previous = NameObject.CHARSETS
        # pypdf's annotation is the type of its default, a tuple of exactly three
        NameObject.CHARSETS = charsets  # type: ignore[assignment]
        try:
            yield
        finally:
            NameObject.CHARSETS = previous
