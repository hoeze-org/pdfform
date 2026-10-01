from __future__ import annotations

import io

import pytest
from formbuilder import field_dict, read
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, NumberObject, RectangleObject

from pdfform import FieldValueError, extract_form, stamp_signature
from pdfform.position import POINTS_PER_MM as MM
from pdfform.position import page_rect


def a4(*, rotate: int = 0, crop: tuple[float, float, float, float] | None = None) -> PdfWriter:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    if rotate:
        page.rotate(rotate)
    if crop is not None:
        page.cropbox = RectangleObject(crop)
    return writer


def as_bytes(writer: PdfWriter) -> bytes:
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def pdf_image() -> bytes:
    """A signature as a one-page PDF, which needs no optional dependency."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=25)
    content = DecodedStreamObject()
    content.set_data(b"0 g 0 0 m 100 25 l S")
    page[NameObject("/Contents")] = writer._add_object(content)
    return as_bytes(writer)


def test_points_are_user_space_as_given():
    assert page_rect(a4(), 0, (250, 680, 50, 640)) == (50, 640, 250, 680)


def test_millimetres_are_measured_from_the_top_left():
    assert page_rect(a4(), 0, (10, 20, 60, 40), "mm") == pytest.approx((10 * MM, 842 - 40 * MM, 60 * MM, 842 - 20 * MM))


def test_millimetres_start_at_the_crop_box():
    writer = a4(crop=(100, 100, 495, 742))
    assert page_rect(writer, 0, (0, 0, 10, 10), "mm") == pytest.approx((100, 742 - 10 * MM, 100 + 10 * MM, 742))


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (0, 842 - 10 * MM, 30 * MM, 842)),
        (90, (0, 0, 10 * MM, 30 * MM)),  # turned clockwise: the bottom left corner is shown at the top left
        (180, (595 - 30 * MM, 0, 595, 10 * MM)),
        (270, (595 - 10 * MM, 842 - 30 * MM, 595, 842)),
    ],
)
def test_millimetres_follow_the_page_as_shown(rotation, expected):
    """A box 30 mm wide and 10 mm tall, at the top left corner of the page as shown."""
    assert page_rect(a4(rotate=rotation), 0, (0, 0, 30, 10), "mm") == pytest.approx(expected)


def test_an_inherited_rotation_counts():
    writer = a4()
    writer.root_object["/Pages"].get_object()[NameObject("/Rotate")] = NumberObject(90)
    assert page_rect(writer, 0, (0, 0, 30, 10), "mm") == pytest.approx((0, 0, 10 * MM, 30 * MM))


def test_no_such_page():
    with pytest.raises(FieldValueError, match=r"no page 3 \(index 2\), the document has 1 page"):
        page_rect(a4(), 2, (0, 0, 10, 10))


def test_no_area():
    with pytest.raises(FieldValueError, match="no area"):
        page_rect(a4(), 0, (10, 10, 10, 50))


@pytest.mark.parametrize(
    ("rect", "units", "size"),
    [((500, 800, 700, 900), "pt", "0,0,595,842 in points"), ((150, 280, 220, 300), "mm", "210 x 297 mm")],
)
def test_off_the_page(rect, units, size):
    with pytest.raises(FieldValueError, match=f"does not lie on page 1, which is {size}"):
        page_rect(a4(), 0, rect, units)


def test_bad_units():
    with pytest.raises(FieldValueError, match="units"):
        page_rect(a4(), 0, (0, 0, 10, 10), "cm")  # type: ignore[arg-type]


def test_stamp_makes_a_new_field_on_a_document_without_a_form(no_form_bytes):
    data = stamp_signature(no_form_bytes, "Unterschrift", pdf_image(), page=0, rect=(20, 20, 120, 60))
    (field,) = extract_form(data).fields
    assert (field.name, field.kind.value, field.pages) == ("Unterschrift", "signature", [0])
    widget = field_dict(data, "Unterschrift")
    assert [float(v) for v in widget["/Rect"]] == [20, 20, 120, 60]
    assert widget.raw_get("/P").idnum == read(data).pages[0].indirect_reference.idnum
    assert widget["/AP"]["/N"].get_object()["/PdfformStamp"]


def test_stamp_adds_a_field_next_to_the_existing_ones(form_bytes):
    data = stamp_signature(form_bytes, "Second", pdf_image(), page=1, rect=(20, 100, 80, 120), units="mm")
    names = {f.name: f for f in extract_form(data).fields}
    assert names["Second"].pages == [1]
    assert {"Name", "Sign"} <= set(names)
    assert names["Sign"].value is None


def test_a_new_field_on_a_turned_page_is_turned_with_it():
    data = stamp_signature(
        as_bytes(a4(rotate=90)), "Unterschrift", pdf_image(), page=0, rect=(10, 10, 80, 30), units="mm"
    )
    widget = field_dict(data, "Unterschrift")
    assert widget["/MK"]["/R"] == 90
    assert [float(v) for v in widget["/AP"]["/N"].get_object()["/Matrix"]] == [0, 1, -1, 0, 0, 0]


def test_stamp_refuses_to_make_a_field_that_exists(form_bytes):
    with pytest.raises(FieldValueError, match="exists already"):
        stamp_signature(form_bytes, "Sign", pdf_image(), page=1, rect=(20, 20, 120, 60))


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"rect": (20, 20, 120, 60)}])
def test_stamp_needs_page_and_rect_together(no_form_bytes, kwargs):
    with pytest.raises(FieldValueError, match="both a page and a rectangle"):
        stamp_signature(no_form_bytes, "Unterschrift", pdf_image(), **kwargs)


def test_a_new_field_name_has_no_dot(no_form_bytes):
    with pytest.raises(FieldValueError, match="without '.'"):
        stamp_signature(no_form_bytes, "a.b", pdf_image(), page=0, rect=(20, 20, 120, 60))


@pytest.fixture
def cli():
    from click.testing import CliRunner

    from pdfform.cli import main_cli

    return lambda *args: CliRunner().invoke(main_cli, [str(a) for a in args])


def test_cli_stamp_at_a_place_in_millimetres(cli, form_path, tmp_path):
    image = tmp_path / "sig.pdf"
    image.write_bytes(pdf_image())
    out = tmp_path / "out.pdf"
    result = cli(
        "stamp", form_path, "--field", "Unterschrift", "--image", image,
        "--page", "2", "--rect", "18mm,230mm,88mm,250mm", "-o", out,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    rect = [float(v) for v in field_dict(out.read_bytes(), "Unterschrift")["/Rect"]]
    assert rect == pytest.approx([18 * MM, 842 - 250 * MM, 88 * MM, 842 - 230 * MM])
    assert {f.name: f.pages for f in extract_form(out.read_bytes()).fields}["Unterschrift"] == [1]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--page", "1", "--rect", "1,2,3"), "X1,Y1,X2,Y2"),
        (("--page", "1", "--rect", "1mm,2,3,4"), "all four values in mm"),
        (("--page", "1", "--rect", "a,b,c,d"), "four numbers"),
        (("--rect", "1,2,3,4"), "--page and --rect go together"),
        (("--page", "1"), "--page and --rect go together"),
    ],
)
def test_cli_rejects_a_bad_place(cli, form_path, tmp_path, args, message):
    image = tmp_path / "sig.pdf"
    image.write_bytes(pdf_image())
    result = cli("stamp", form_path, "--field", "New", "--image", image, *args, "-o", tmp_path / "o.pdf")
    assert result.exit_code == 1
    assert message in result.output


def test_cli_pages_count_from_one(cli, form_path, tmp_path):
    image = tmp_path / "sig.pdf"
    image.write_bytes(pdf_image())
    result = cli("stamp", form_path, "--field", "New", "--image", image, "--page", "0", "--rect", "1,1,9,9", "-o", "o")
    assert result.exit_code == 2  # click rejects the value
