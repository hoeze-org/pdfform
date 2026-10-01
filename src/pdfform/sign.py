"""Sign a PDF cryptographically, as a PAdES incremental update.

This is separate from :mod:`pdfform.stamp`, which only draws an image. Signing
needs a certificate and a private key, and appends to the file instead of
rewriting it, so everything signed earlier stays valid. Whatever changes the
document afterwards, ``fill`` included, breaks the signature. Sign last.

pyHanko does the work and is imported lazily, because it is an optional
dependency: ``pip install 'pdfform[sign]'``.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pdfform.extract import extract_form_and_objects, open_pdf
from pdfform.fill import _match_field, _signed_fields
from pdfform.model import FieldKind, MissingDependencyError, SigningError
from pdfform.stamp import STAMP_MARKER

logger = logging.getLogger(__name__)

DEFAULT_FIELD_PREFIX = "Signature"


class _KeepAppearance:
    """A stamp style that leaves the field's existing appearance alone.

    pyHanko draws a text appearance into every visible field it signs, which would
    cover a signature image placed by ``stamp``. When a style returns no stamp, it
    leaves the field as it is.
    """

    def create_stamp(self, writer: Any, box: Any, text_params: Any) -> None:
        return None


def _passphrase(value: str | bytes | None) -> bytes | None:
    if value is None:
        return None
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _with_passphrase(load: Callable[[bytes | None], Any], passphrase: bytes | None) -> Any:
    """Call *load* with *passphrase*, and without one if that fails.

    ``PDFFORM_PASSPHRASE`` applies to every key, so a key without a passphrase
    has to load even when one is given. If both attempts fail, the first error counts.
    """
    try:
        return load(passphrase)
    except Exception as first:
        if passphrase is None:
            raise
        try:
            return load(None)
        except Exception:
            raise first  # noqa: B904 - the error with the passphrase is the one to report


def _load_signer(
    signers: Any,
    pkcs12: Path | str | None,
    key: Path | str | None,
    cert: Path | str | None,
    chain: Sequence[Path | str],
    passphrase: bytes | None,
) -> Any:
    from pyhanko.keys import load_cert_from_pemder, load_certs_from_pemder, load_private_key_from_pemder
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    if pkcs12 is not None and (key is not None or cert is not None):
        raise SigningError("Give either a PKCS#12 file or a key and a certificate, not both")
    if pkcs12 is None and (key is None or cert is None):
        raise SigningError("Signing needs a PKCS#12 file, or a key and a certificate")
    # The *_data and pemder loaders raise. SimpleSigner.load and load_pkcs12 log a traceback and return None.
    try:
        others = list(load_certs_from_pemder([str(c) for c in chain]))
        if pkcs12 is not None:
            data = Path(pkcs12).read_bytes()
            return _with_passphrase(lambda p: signers.SimpleSigner.load_pkcs12_data(data, others, p), passphrase)
        assert key is not None and cert is not None
        signing_key = _with_passphrase(lambda p: load_private_key_from_pemder(str(key), p), passphrase)
        return signers.SimpleSigner(
            signing_cert=load_cert_from_pemder(str(cert)),
            signing_key=signing_key,
            cert_registry=SimpleCertificateStore.from_certs(others),
        )
    except Exception as exc:
        # pyHanko wraps the useful message, "Invalid password or PKCS12 data" for example.
        reason = exc.__cause__ or exc
        raise SigningError(f"Could not load the signing key and certificate: {reason}") from exc


def _stamped(widget: Any) -> bool:
    """Whether the appearance of *widget* was drawn by ``stamp``."""
    appearance = widget.get("/AP")
    normal = appearance.get_object().get("/N") if appearance is not None else None
    return normal is not None and bool(normal.get_object().get(STAMP_MARKER))


def _new_field_name(taken: set[str]) -> str:
    number = 1
    while f"{DEFAULT_FIELD_PREFIX}{number}" in taken:
        number += 1
    return f"{DEFAULT_FIELD_PREFIX}{number}"


def sign_pdf(
    source: Any,
    output: Any = None,
    *,
    pkcs12: Path | str | None = None,
    key: Path | str | None = None,
    cert: Path | str | None = None,
    chain: Sequence[Path | str] = (),
    passphrase: str | bytes | None = None,
    field: str | None = None,
    reason: str | None = None,
    location: str | None = None,
    contact: str | None = None,
) -> bytes:
    """Sign a PDF with a certificate and return the signed document as bytes.

    Args:
        source: Path or bytes of the document.
        output: Optional path or writable binary stream. The bytes are returned either way.
        pkcs12: A ``.p12``/``.pfx`` file holding key and certificate. Alternative to *key* and *cert*.
        key: PEM private key. Needs *cert*.
        cert: PEM certificate of the signer.
        chain: PEM certificates of the intermediates.
        passphrase: Passphrase of the PKCS#12 file or the PEM key, if it has one.
        field: Signature field to sign. Without it, a document with exactly one
            unsigned signature field signs that one. A document without any gets a
            new invisible ``Signature1`` (or ``Signature2``, and so on, if taken).
            Several unsigned fields are an error. A signature image placed with
            ``stamp`` stays visible. Any other field gets pyHanko's text appearance.
        reason: Why the document is signed. Shown by viewers.
        location: Where it was signed.
        contact: How to reach the signer.

    Raises:
        MissingDependencyError: pyHanko is not installed.
        UnknownFieldError: *field* names no field, or is ambiguous.
        SigningError: The key could not be loaded, *field* is not a signature field
            or is signed already, several fields are unsigned, or pyHanko refused to sign.
    """
    try:
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign import fields, signers
    except ImportError as exc:
        raise MissingDependencyError(
            "Signing needs the optional dependency 'pyhanko': pip install 'pdfform[sign]'"
        ) from exc

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    signer = _load_signer(signers, pkcs12, key, cert, chain, _passphrase(passphrase))

    info, objects = extract_form_and_objects(open_pdf(data))
    already_signed = set(_signed_fields(info))
    unsigned = [f for f in info.fields if f.kind is FieldKind.SIGNATURE and f.name not in already_signed]
    new_field_spec = None
    if field is not None:
        target = _match_field(field, info, {f.name: f for f in info.fields})
        if target.kind is not FieldKind.SIGNATURE:
            raise SigningError(f"{target.name}: is a {target.kind.value} field, only signature fields can be signed")
        if target.name in already_signed:
            raise SigningError(f"{target.name}: is signed already")
        name = target.name
    elif len(unsigned) == 1:
        name = unsigned[0].name
    elif not unsigned:
        name = _new_field_name({f.name for f in info.fields})
        new_field_spec = fields.SigFieldSpec(sig_field_name=name)
        if already_signed:
            logger.warning(
                "Every signature field is signed already, so this signature goes into a new invisible %s", name
            )
    else:
        raise SigningError(
            "The document has several unsigned signature fields, pick one: " + ", ".join(f.name for f in unsigned)
        )

    style: Any = None
    if new_field_spec is None and any(_stamped(widget) for widget in objects[name][1]):
        style = _KeepAppearance()

    metadata = signers.PdfSignatureMetadata(
        field_name=name,
        reason=reason,
        location=location,
        contact_info=contact,
        subfilter=fields.SigSeedSubFilter.PADES,
    )
    pdf_signer = signers.PdfSigner(metadata, signer, stamp_style=style, new_field_spec=new_field_spec)
    buffer = io.BytesIO()
    try:
        pdf_signer.sign_pdf(
            IncrementalPdfFileWriter(io.BytesIO(data)),
            existing_fields_only=new_field_spec is None,
            output=buffer,
        )
    except Exception as exc:
        raise SigningError(f"Could not sign the document: {exc}") from exc

    signed = buffer.getvalue()
    logger.info("Signed %s", name)
    if output is None:
        return signed
    if isinstance(output, (str, Path)):
        Path(output).write_bytes(signed)
    else:
        output.write(signed)
    return signed
