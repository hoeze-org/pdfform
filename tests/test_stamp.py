from __future__ import annotations

import io

import pytest
from formbuilder import field_dict, read
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from pdfform import (
    FieldValueError,
    SignatureImageError,
    UnknownFieldError,
    flatten_widgets,
    stamp_signature,
)

PIL = pytest.importorskip("PIL.Image")
pytest.importorskip("svglib")

SIGN_RECT = (50, 640, 250, 680)  # 200 x 40, see formbuilder


def png(width: int, height: int, *, alpha: bool = True) -> bytes:
    colour = (20, 20, 120, 255) if alpha else (20, 20, 120)
    image = PIL.new("RGBA" if alpha else "RGB", (width, height), colour)
    if alpha:
        image.putpixel((0, 0), (0, 0, 0, 0))  # something transparent, so an /SMask is needed
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    PIL.new("RGB", (width, height), (10, 10, 10)).save(buffer, "JPEG")
    return buffer.getvalue()


def svg(width: int, height: int) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        f'<path d="M0 0 L{width} {height}" stroke="black" fill="none"/></svg>'
    ).encode()


def pdf_signature(width: int = 100, height: int = 50, *, origin: tuple[int, int] = (0, 0)) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    page.mediabox.lower_left = origin
    page.mediabox.upper_right = (origin[0] + width, origin[1] + height)
    content = DecodedStreamObject()
    content.set_data(f"0 g {origin[0]} {origin[1]} m {origin[0] + width} {origin[1] + height} l S".encode())
    page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def drawn_box(data: bytes, name: str = "Sign") -> tuple[float, float, float, float]:
    """Where the signature lands in the widget's own coordinates, after both nested transforms."""
    widget = field_dict(data, name)
    appearance = widget["/AP"]["/N"].get_object()
    assert [float(v) for v in appearance["/BBox"]] == [0, 0, SIGN_RECT[2] - SIGN_RECT[0], SIGN_RECT[3] - SIGN_RECT[1]]
    a, b, c, d, e, f = (float(v) for v in appearance.get_data().decode().split()[1:7])
    assert (b, c) == (0, 0)
    inner = appearance["/Resources"]["/XObject"]["/Sig"].get_object()
    x0, y0, x1, y1 = (float(v) for v in inner["/BBox"])
    matrix = [float(v) for v in inner["/Matrix"]] if "/Matrix" in inner else [1, 0, 0, 1, 0, 0]
    ia, ib, ic, id_, ie, if_ = matrix
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    inside = [(ia * x + ic * y + ie, ib * x + id_ * y + if_) for x, y in corners]
    xs = [a * x + e for x, _ in inside]
    ys = [d * y + f for _, y in inside]
    return (min(xs), min(ys), max(xs), max(ys))


@pytest.mark.parametrize(
    ("image", "aspect"),
    [
        pytest.param(png(400, 100), 4.0, id="png-with-alpha"),
        pytest.param(png(400, 100, alpha=False), 4.0, id="png-opaque"),
        pytest.param(jpeg(300, 100), 3.0, id="jpeg"),
        pytest.param(svg(120, 30), 4.0, id="svg"),
        pytest.param(pdf_signature(100, 25), 4.0, id="pdf"),
        pytest.param(pdf_signature(100, 25, origin=(30, 40)), 4.0, id="pdf-away-from-origin"),
    ],
)
def test_contain_keeps_aspect_ratio_and_centres(form_bytes, image, aspect):
    box = drawn_box(stamp_signature(form_bytes, "Sign", image))
    width, height = box[2] - box[0], box[3] - box[1]
    assert width / height == pytest.approx(aspect, rel=1e-3)
    assert box[0] >= -1e-3 and box[1] >= -1e-3 and box[2] <= 200 + 1e-3 and box[3] <= 40 + 1e-3
    assert (box[0] + box[2]) / 2 == pytest.approx(100)
    assert (box[1] + box[3]) / 2 == pytest.approx(20)
    # the limiting side touches the edge
    assert width == pytest.approx(200) or height == pytest.approx(40)


def test_stretch_fills_the_field(form_bytes):
    box = drawn_box(stamp_signature(form_bytes, "Sign", png(10, 100), fit="stretch"))
    assert box == pytest.approx((0, 0, 200, 40))


def test_rotated_pdf_page_is_placed_upright(form_bytes):
    writer = PdfWriter(clone_from=io.BytesIO(pdf_signature(100, 25)))
    writer.pages[0].rotate(90)
    buffer = io.BytesIO()
    writer.write(buffer)
    box = drawn_box(stamp_signature(form_bytes, "Sign", buffer.getvalue()))
    assert (box[2] - box[0]) / (box[3] - box[1]) == pytest.approx(0.25, rel=1e-3)


def test_raster_alpha_becomes_a_soft_mask(form_bytes):
    data = stamp_signature(form_bytes, "Sign", png(40, 10))
    appearance = field_dict(data, "Sign")["/AP"]["/N"].get_object()
    inner = appearance["/Resources"]["/XObject"]["/Sig"].get_object()
    image = inner["/Resources"]["/XObject"]["/Img"].get_object()
    assert image["/ColorSpace"] == "/DeviceRGB"
    assert image["/SMask"].get_object()["/ColorSpace"] == "/DeviceGray"


def test_opaque_png_gets_no_soft_mask(form_bytes):
    data = stamp_signature(form_bytes, "Sign", png(40, 10, alpha=False))
    inner = field_dict(data, "Sign")["/AP"]["/N"].get_object()["/Resources"]["/XObject"]["/Sig"].get_object()
    assert "/SMask" not in inner["/Resources"]["/XObject"]["/Img"].get_object()


def test_the_field_stays_a_signature_field_without_a_value(form_bytes):
    widget = field_dict(stamp_signature(form_bytes, "Sign", png(40, 10)), "Sign")
    assert widget["/FT"] == "/Sig"
    assert "/V" not in widget


def test_other_fields_are_untouched(form_bytes):
    before = read(form_bytes).get_fields()
    after = read(stamp_signature(form_bytes, "Sign", png(40, 10))).get_fields()
    assert set(before) == set(after)
    assert {k: v.get("/V") for k, v in before.items()} == {k: v.get("/V") for k, v in after.items()}


def test_writes_to_a_path_and_returns_the_bytes(form_bytes, tmp_path):
    target = tmp_path / "out.pdf"
    data = stamp_signature(form_bytes, "Sign", png(40, 10), target)
    assert target.read_bytes() == data


def test_image_can_be_a_path(form_bytes, tmp_path):
    image = tmp_path / "sig.png"
    image.write_bytes(png(40, 10))
    assert drawn_box(stamp_signature(form_bytes, "Sign", image))


def test_a_stamped_field_is_baked_in_by_flatten(form_bytes):
    stamped = stamp_signature(form_bytes, "Sign", png(40, 10))
    plain = PdfWriter(clone_from=io.BytesIO(form_bytes))
    marked = PdfWriter(clone_from=io.BytesIO(stamped))
    assert flatten_widgets(marked) == flatten_widgets(plain) + 1


def test_restamping_replaces_the_appearance(form_bytes):
    once = stamp_signature(form_bytes, "Sign", png(400, 100))
    twice = stamp_signature(once, "Sign", png(100, 100))
    box = drawn_box(twice)
    assert box[2] - box[0] == pytest.approx(40)


def test_refuses_a_field_that_is_not_a_signature(form_bytes):
    with pytest.raises(FieldValueError, match="only signature fields"):
        stamp_signature(form_bytes, "Name", png(40, 10))


def test_unknown_field(form_bytes):
    with pytest.raises(UnknownFieldError):
        stamp_signature(form_bytes, "Nope", png(40, 10))


@pytest.mark.parametrize("junk", [b"", b"not an image at all"])
def test_unreadable_image(form_bytes, junk):
    with pytest.raises(SignatureImageError):
        stamp_signature(form_bytes, "Sign", junk)


def test_broken_svg(form_bytes):
    with pytest.raises(SignatureImageError):
        stamp_signature(form_bytes, "Sign", b"<svg><</svg>")


def test_bad_fit(form_bytes):
    with pytest.raises(FieldValueError, match="fit"):
        stamp_signature(form_bytes, "Sign", png(40, 10), fit="cover")  # type: ignore[arg-type]


def test_cli_stamp(form_path, tmp_path):
    from click.testing import CliRunner

    from pdfform.cli import main_cli

    image = tmp_path / "sig.png"
    image.write_bytes(png(400, 100))
    out = tmp_path / "out.pdf"
    result = CliRunner().invoke(
        main_cli,
        ["stamp", str(form_path), "--field", "Sign", "--image", str(image), "-o", str(out), "--fit", "stretch"],
    )
    assert result.exit_code == 0, result.output
    assert drawn_box(out.read_bytes()) == pytest.approx((0, 0, 200, 40))


def test_cli_stamp_reports_a_wrong_field_without_a_traceback(form_path, tmp_path):
    from click.testing import CliRunner

    from pdfform.cli import main_cli

    image = tmp_path / "sig.png"
    image.write_bytes(png(40, 10))
    result = CliRunner().invoke(
        main_cli, ["stamp", str(form_path), "--field", "Name", "--image", str(image), "-o", str(tmp_path / "o.pdf")]
    )
    assert result.exit_code == 2
    assert "only signature fields" in result.output
