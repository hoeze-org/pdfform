from __future__ import annotations

import datetime
import io

import pytest
from formbuilder import field_dict, read
from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

from pdfform import MissingDependencyError, SigningError, UnknownFieldError, sign_pdf, stamp_signature

pytest.importorskip("pyhanko")
x509 = pytest.importorskip("cryptography.x509")

from asn1crypto import x509 as asn1_x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: E402
from pyhanko.sign.validation import validate_pdf_signature  # noqa: E402
from pyhanko_certvalidator import ValidationContext  # noqa: E402

PASSPHRASE = "correct horse"


@pytest.fixture(scope="module")
def identity():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


@pytest.fixture
def files(tmp_path, identity):
    key, cert = identity
    encrypted = serialization.BestAvailableEncryption(PASSPHRASE.encode())
    paths = {
        "cert": tmp_path / "cert.pem",
        "key": tmp_path / "key.pem",
        "key_plain": tmp_path / "key-plain.pem",
        "p12": tmp_path / "id.p12",
        "p12_plain": tmp_path / "id-plain.p12",
    }
    paths["cert"].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem_args = (serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8)
    paths["key"].write_bytes(key.private_bytes(*pem_args, encrypted))
    paths["key_plain"].write_bytes(key.private_bytes(*pem_args, serialization.NoEncryption()))
    paths["p12"].write_bytes(pkcs12.serialize_key_and_certificates(b"id", key, cert, None, encrypted))
    paths["p12_plain"].write_bytes(
        pkcs12.serialize_key_and_certificates(b"id", key, cert, None, serialization.NoEncryption())
    )
    return paths


def check(signed: bytes, cert, *, count: int = 1):
    """Validate every signature against *cert* as the only trust root."""
    der = asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    context = ValidationContext(trust_roots=[der])
    signatures = PdfFileReader(io.BytesIO(signed)).embedded_signatures
    assert len(signatures) == count
    statuses = [validate_pdf_signature(s, signer_validation_context=context) for s in signatures]
    for status in statuses:
        assert status.intact and status.valid and status.trusted
    return signatures, statuses


def test_signs_the_only_signature_field_with_a_pkcs12_file(form_bytes, files, identity):
    signed = sign_pdf(form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE)
    signatures, _ = check(signed, identity[1])
    assert signatures[0].field_name == "Sign"


def test_signs_with_pem_key_and_certificate(form_bytes, files, identity):
    signed = sign_pdf(form_bytes, key=files["key"], cert=files["cert"], passphrase=PASSPHRASE.encode())
    check(signed, identity[1])


def test_key_without_passphrase(form_bytes, files, identity):
    check(sign_pdf(form_bytes, key=files["key_plain"], cert=files["cert"]), identity[1])
    check(sign_pdf(form_bytes, pkcs12=files["p12_plain"]), identity[1])


def test_signing_appends_and_keeps_the_original_bytes(form_bytes, files):
    signed = sign_pdf(form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE)
    assert signed.startswith(form_bytes)


def test_metadata_ends_up_in_the_signature(form_bytes, files):
    signed = sign_pdf(
        form_bytes,
        pkcs12=files["p12"],
        passphrase=PASSPHRASE,
        reason="Approved",
        location="Munich",
        contact="me@example.org",
    )
    sig = field_dict(signed, "Sign")["/V"].get_object()
    assert (str(sig["/Reason"]), str(sig["/Location"]), str(sig["/ContactInfo"])) == (
        "Approved",
        "Munich",
        "me@example.org",
    )


def test_a_stamped_signature_stays_visible(form_bytes, files, identity):
    image = io.BytesIO()
    pytest.importorskip("PIL")
    from PIL import Image

    Image.new("RGB", (40, 10), (0, 0, 0)).save(image, "PNG")
    stamped = stamp_signature(form_bytes, "Sign", image.getvalue())
    before = field_dict(stamped, "Sign")["/AP"]["/N"].get_data()

    signed = sign_pdf(stamped, pkcs12=files["p12"], passphrase=PASSPHRASE)

    check(signed, identity[1])
    assert field_dict(signed, "Sign")["/AP"]["/N"].get_data() == before


def test_an_unstamped_field_gets_the_default_appearance(form_bytes, files):
    signed = sign_pdf(form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE)
    assert field_dict(signed, "Sign")["/AP"]["/N"].get_data()


def test_a_document_without_a_signature_field_gets_an_invisible_one(no_form_bytes, files, identity):
    signed = sign_pdf(no_form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE)
    signatures, _ = check(signed, identity[1])
    assert signatures[0].field_name == "Signature1"


def two_signature_fields(form_bytes: bytes) -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(form_bytes))
    acro = writer.root_object["/AcroForm"]
    original = field_dict(form_bytes, "Sign")
    clone = original.clone(writer)
    clone[NameObject("/T")] = TextStringObject("Sign2")
    ref = writer._add_object(clone)
    acro["/Fields"].append(ref)
    writer.pages[1]["/Annots"].append(ref)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_several_signature_fields_need_a_choice(form_bytes, files):
    with pytest.raises(SigningError, match="Sign, Sign2"):
        sign_pdf(two_signature_fields(form_bytes), pkcs12=files["p12"], passphrase=PASSPHRASE)


def test_two_signatures_in_two_fields(form_bytes, files, identity):
    both = two_signature_fields(form_bytes)
    once = sign_pdf(both, pkcs12=files["p12"], passphrase=PASSPHRASE, field="Sign")
    twice = sign_pdf(once, pkcs12=files["p12"], passphrase=PASSPHRASE, field="Sign2")
    signatures = PdfFileReader(io.BytesIO(twice)).embedded_signatures
    assert [s.field_name for s in signatures] == ["Sign", "Sign2"]
    der = asn1_x509.Certificate.load(identity[1].public_bytes(serialization.Encoding.DER))
    context = ValidationContext(trust_roots=[der])
    # skip_diff: the hand-built form has a check box whose /AP /N is a direct dictionary,
    # which pyHanko's revision analysis cannot follow. The digest check is what matters here.
    first = validate_pdf_signature(signatures[0], signer_validation_context=context, skip_diff=True)
    assert first.intact and first.valid and first.trusted  # the second signature did not disturb the first


def test_signing_the_same_field_twice_is_refused(form_bytes, files):
    once = sign_pdf(form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE)
    with pytest.raises(SigningError):
        sign_pdf(once, pkcs12=files["p12"], passphrase=PASSPHRASE, field="Sign")


def test_field_must_be_a_signature_field(form_bytes, files):
    with pytest.raises(SigningError, match="only signature fields"):
        sign_pdf(form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE, field="Name")


def test_unknown_field(form_bytes, files):
    with pytest.raises(UnknownFieldError):
        sign_pdf(form_bytes, pkcs12=files["p12"], passphrase=PASSPHRASE, field="Nope")


def test_wrong_passphrase(form_bytes, files):
    with pytest.raises(SigningError, match="Could not load"):
        sign_pdf(form_bytes, pkcs12=files["p12"], passphrase="wrong")


def test_missing_passphrase(form_bytes, files):
    with pytest.raises(SigningError, match="Could not load"):
        sign_pdf(form_bytes, pkcs12=files["p12"])


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"key": "k.pem"}, {"cert": "c.pem"}, {"pkcs12": "a.p12", "key": "k.pem", "cert": "c.pem"}],
)
def test_needs_exactly_one_kind_of_credentials(form_bytes, kwargs):
    with pytest.raises(SigningError):
        sign_pdf(form_bytes, **kwargs)


def test_signing_without_pyhanko_says_how_to_install_it(form_bytes, monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("pyhanko"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(MissingDependencyError, match=r"pdfform\[sign\]"):
        sign_pdf(form_bytes)


def test_output_path(form_bytes, files, tmp_path):
    target = tmp_path / "signed.pdf"
    data = sign_pdf(form_bytes, target, pkcs12=files["p12"], passphrase=PASSPHRASE)
    assert target.read_bytes() == data
    assert read(data).get_fields()["Sign"]["/V"]


@pytest.fixture
def cli():
    from click.testing import CliRunner

    from pdfform.cli import main_cli

    return lambda *args, **kwargs: CliRunner().invoke(main_cli, [str(a) for a in args], **kwargs)


def test_cli_sign_reads_the_passphrase_from_a_file(cli, form_path, files, identity, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text(PASSPHRASE + "\n")
    out = tmp_path / "signed.pdf"
    result = cli("sign", form_path, "--p12", files["p12"], "--passphrase-file", secret, "-o", out)
    assert result.exit_code == 0, result.output
    check(out.read_bytes(), identity[1])


def test_cli_sign_reads_the_passphrase_from_the_environment(cli, form_path, files, identity, tmp_path):
    out = tmp_path / "signed.pdf"
    result = cli("sign", form_path, "--p12", files["p12"], "-o", out, env={"PDFFORM_PASSPHRASE": PASSPHRASE})
    assert result.exit_code == 0, result.output
    check(out.read_bytes(), identity[1])


def test_cli_sign_can_prompt_for_the_passphrase(cli, form_path, files, identity, tmp_path):
    out = tmp_path / "signed.pdf"
    result = cli(
        "sign",
        form_path,
        "--key",
        files["key"],
        "--cert",
        files["cert"],
        "--ask-passphrase",
        "-o",
        out,
        input=PASSPHRASE + "\n",
    )
    assert result.exit_code == 0, result.output
    assert PASSPHRASE not in result.output
    check(out.read_bytes(), identity[1])


def test_cli_sign_reports_a_wrong_passphrase_cleanly(cli, form_path, files, tmp_path):
    result = cli("sign", form_path, "--p12", files["p12"], "-o", tmp_path / "o.pdf", env={"PDFFORM_PASSPHRASE": "nope"})
    assert result.exit_code == 2
    assert "Could not load" in result.output
    assert not (tmp_path / "o.pdf").exists()


def test_the_passphrase_is_not_a_command_line_option(cli, form_path, files, tmp_path):
    result = cli("sign", form_path, "--p12", files["p12"], "--passphrase", PASSPHRASE, "-o", tmp_path / "o.pdf")
    assert result.exit_code == 2


def test_cli_fill_stamps_and_signs_in_one_go(cli, form_path, files, identity, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    image = tmp_path / "sig.png"
    Image.new("RGB", (40, 10), (0, 0, 0)).save(image)
    out = tmp_path / "out.pdf"
    result = cli(
        "fill", form_path, "--set", "Name=Doe", "--stamp", f"Sign={image}",
        "--sign", "--p12", files["p12"], "--sign-field", "Sign", "--reason", "Approved", "-o", out,
        env={"PDFFORM_PASSPHRASE": PASSPHRASE},
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    signed = out.read_bytes()
    signatures, _ = check(signed, identity[1])
    assert signatures[0].field_name == "Sign"
    assert str(field_dict(signed, "Name")["/V"]) == "Doe"
    assert field_dict(signed, "Sign")["/AP"]["/N"].get_data().startswith(b"q ")  # the stamp survived signing
    assert str(field_dict(signed, "Sign")["/V"].get_object()["/Reason"]) == "Approved"


def test_cli_fill_can_sign_without_stamping(cli, form_path, files, identity, tmp_path):
    out = tmp_path / "out.pdf"
    result = cli(
        "fill",
        form_path,
        "--set",
        "Name=Doe",
        "--sign",
        "--p12",
        files["p12"],
        "-o",
        out,
        env={"PDFFORM_PASSPHRASE": PASSPHRASE},
    )
    assert result.exit_code == 0, result.output
    check(out.read_bytes(), identity[1])


def test_cli_fill_with_flatten_signs_an_invisible_field(cli, form_path, files, identity, tmp_path):
    out = tmp_path / "out.pdf"
    result = cli(
        "fill",
        form_path,
        "--set",
        "Name=Doe",
        "--flatten",
        "--sign",
        "--p12",
        files["p12"],
        "-o",
        out,
        env={"PDFFORM_PASSPHRASE": PASSPHRASE},
    )
    assert result.exit_code == 0, result.output
    signatures, _ = check(out.read_bytes(), identity[1])
    assert signatures[0].field_name == "Signature1"


def test_cli_fill_signing_options_need_sign(cli, form_path, files, tmp_path):
    result = cli("fill", form_path, "--set", "Name=Doe", "--p12", files["p12"], "-o", tmp_path / "o.pdf")
    assert result.exit_code == 1
    assert "--sign" in result.output
    assert not (tmp_path / "o.pdf").exists()


def test_cli_fill_does_not_write_an_unsigned_file_when_signing_fails(cli, form_path, files, tmp_path):
    out = tmp_path / "o.pdf"
    result = cli(
        "fill",
        form_path,
        "--set",
        "Name=Doe",
        "--sign",
        "--p12",
        files["p12"],
        "-o",
        out,
        env={"PDFFORM_PASSPHRASE": "wrong"},
    )
    assert result.exit_code == 2
    assert not out.exists()
