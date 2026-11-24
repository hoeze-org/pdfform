"""Decoding of names that are not UTF-8, such as a radio state in Latin-1."""

from __future__ import annotations

import codecs
import io
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from formbuilder import LATIN1_STATE, build_form, field_dict
from pypdf import PdfWriter
from pypdf.generic import NameObject

from pdfform import extract_form, fill_form, name_charsets, normalize_names, stamp_signature, strip_xfa_layer
from pdfform.model import FieldValueError, XfaKind
from pdfform.names import CODE_PAGES, code_page


def states(data: bytes) -> list[str]:
    return extract_form(data).by_name("Direction").on_states


@pytest.mark.parametrize(
    ("lang", "page"),
    [
        (None, "cp1252"),
        ("", "cp1252"),
        ("de-DE", "cp1252"),
        ("en_US", "cp1252"),
        ("CS", "cp1250"),
        ("ru", "cp1251"),
        ("sr", "cp1251"),
        ("sr-Latn-RS", "cp1250"),
        ("ja", "cp932"),
        ("zh-CN", "gbk"),
        ("zh-TW", "cp950"),
        ("zh-Hant", "cp950"),
        ("zh-Hans-HK", "gbk"),  # the script decides before the region
        ("bs-Cyrl-BA", "cp1251"),
    ],
)
def test_code_page_follows_the_lang_tag(lang, page):
    assert code_page(lang) == page


def test_every_code_page_is_a_python_codec():
    for page in set(CODE_PAGES.values()):
        codecs.lookup(page)


def test_latin1_state_is_decoded_without_lang(form_bytes):
    assert states(form_bytes) == ["Hinfahrt", "Rückfahrt"]


@pytest.mark.parametrize("objstm", [False, True])
def test_chinese_lang_keeps_gbk(objstm):
    assert states(build_form(lang="zh-CN", objstm=objstm)) == ["Hinfahrt", "R點kfahrt"]


@pytest.mark.parametrize("objstm", [False, True])
def test_explicit_charsets_win_over_lang(objstm):
    with name_charsets("utf-8", "latin1"):
        assert states(build_form(lang="zh-CN", objstm=objstm)) == ["Hinfahrt", "Rückfahrt"]
    with name_charsets("utf-8", "gbk", "latin1"):
        assert states(build_form(objstm=objstm)) == ["Hinfahrt", "R點kfahrt"]


def test_parallel_calls_keep_their_own_charsets():
    documents = {lang: build_form(lang=lang, objstm=True) for lang in ("de", "ru", "zh-CN")}
    expected = {"de": "Rückfahrt", "ru": "Rьckfahrt", "zh-CN": "R點kfahrt"}
    before = NameObject.CHARSETS
    with ThreadPoolExecutor(8) as pool:
        seen = list(pool.map(lambda lang: (lang, states(documents[lang])[1]), list(documents) * 30))
    assert {lang: state for lang, state in seen} == expected
    assert all(state == expected[lang] for lang, state in seen)
    assert NameObject.CHARSETS == before


def test_unknown_charset_is_rejected():
    with pytest.raises(ValueError, match="nope"), name_charsets("utf-8", "nope"):
        pass
    with pytest.raises(ValueError, match="at least one"), name_charsets():
        pass


def test_pypdf_charsets_are_restored(form_bytes):
    before = NameObject.CHARSETS
    fill_form(form_bytes, {"Direction": "Rückfahrt"})
    assert NameObject.CHARSETS == before


def test_fill_writes_the_state_as_utf8(form_bytes):
    out = fill_form(form_bytes, {"Direction": "Rückfahrt"})
    assert LATIN1_STATE not in out
    # /V, the kid's /AS and its /AP /N key all carry the same bytes
    assert out.count(b"/R#C3#BCckfahrt") == 3
    # so a reader with pypdf's own charsets reads it back correctly
    assert str(field_dict(out, "Direction")["/V"]) == "/Rückfahrt"


def test_fill_rejects_the_garbled_spelling(form_bytes):
    with pytest.raises(FieldValueError, match="Rückfahrt"):
        fill_form(form_bytes, {"Direction": "R點kfahrt"})


def test_normalize_writes_every_name_as_utf8(form_bytes):
    out = normalize_names(form_bytes)
    assert LATIN1_STATE not in out
    assert b"/R#C3#BCckfahrt" in out
    assert [f.name for f in extract_form(out)] == [f.name for f in extract_form(form_bytes)]


@pytest.mark.parametrize("objstm", [False, True])
def test_normalize_follows_lang(objstm):
    out = normalize_names(build_form(lang="zh-CN", objstm=objstm))
    assert b"/R#E9#BB#9Ekfahrt" in out  # "R點kfahrt", as GBK decodes the Latin-1 bytes


def test_normalize_keeps_the_xfa_layer(hybrid_bytes):
    assert extract_form(normalize_names(hybrid_bytes)).xfa is XfaKind.HYBRID


def test_strip_xfa_writes_the_state_as_utf8(form_bytes):
    out = strip_xfa_layer(form_bytes)
    assert LATIN1_STATE not in out
    assert b"/R#C3#BCckfahrt" in out


def test_stamp_writes_the_state_as_utf8(form_bytes):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=50)
    image = io.BytesIO()
    writer.write(image)
    out = stamp_signature(form_bytes, "Sign", image.getvalue())
    assert LATIN1_STATE not in out
    assert b"/R#C3#BCckfahrt" in out


def test_a_trailer_without_root_is_recovered():
    data = build_form()
    head, trailer = data.rsplit(b"trailer", 1)
    broken = head + b"trailer" + re.sub(rb"/Root \d+ 0 R\n", b"", trailer)
    assert b"/R#C3#BCckfahrt" in strip_xfa_layer(broken)
    assert b"/R#C3#BCckfahrt" in normalize_names(broken)


def test_cli_normalize(tmp_path, form_path):
    from click.testing import CliRunner

    from pdfform.cli import main_cli

    out = tmp_path / "out.pdf"
    result = CliRunner().invoke(main_cli, ["normalize", str(form_path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert LATIN1_STATE not in out.read_bytes()


def test_cli_name_charsets(tmp_path, form_path):
    from click.testing import CliRunner

    from pdfform.cli import main_cli

    out = tmp_path / "out.pdf"
    runner = CliRunner()
    ok = runner.invoke(main_cli, ["fill", str(form_path), "--set", "Direction=Rückfahrt", "-o", str(out)])
    assert ok.exit_code == 0, ok.output
    gbk = ["--name-charsets", "utf-8, gbk ,latin1", "fill", str(form_path), "--set", "Direction=Rückfahrt"]
    garbled = runner.invoke(main_cli, [*gbk, "-o", str(out)])
    assert garbled.exit_code == 2
    assert "R點kfahrt" in garbled.output
    bad = runner.invoke(main_cli, ["--name-charsets", "utf-8,nope", "values", str(form_path)])
    assert bad.exit_code == 2
    assert "Unknown charset 'nope'" in bad.output
