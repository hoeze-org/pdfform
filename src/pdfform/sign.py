"""Sign a PDF cryptographically, as a PAdES-style incremental update.

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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pdfform.extract import extract_form_and_objects, open_pdf
from pdfform.fill import _match_field
from pdfform.model import FieldKind, MissingDependencyError, SigningError

logger = logging.getLogger(__name__)

DEFAULT_FIELD = "Signature1"


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


def _load_signer(
    signers: Any,
    pkcs12: Path | str | None,
    key: Path | str | None,
    cert: Path | str | None,
    chain: Sequence[Path | str],
    passphrase: bytes | None,
) -> Any:
    if pkcs12 is not None and (key is not None or cert is not None):
        raise SigningError("Give either a PKCS#12 file or a key and a certificate, not both")
    if pkcs12 is None and (key is None or cert is None):
        raise SigningError("Signing needs a PKCS#12 file, or a key and a certificate")
    try:
        if pkcs12 is not None:
            signer = signers.SimpleSigner.load_pkcs12(
                str(pkcs12), ca_chain_files=[str(c) for c in chain], passphrase=passphrase
            )
        else:
            assert key is not None and cert is not None
            signer = signers.SimpleSigner.load(
                str(key), str(cert), ca_chain_files=[str(c) for c in chain], key_passphrase=passphrase
            )
    except Exception as exc:
        raise SigningError(f"Could not load the signing key and certificate: {exc}") from exc
    if signer is None:  # pyHanko reports a wrong passphrase this way in some versions
        raise SigningError("Could not load the signing key and certificate, is the passphrase right?")
    return signer


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
        field: Signature field to sign. Without it, a form with exactly one signature
            field signs that one, a document without any gets an invisible
            ``Signature1``, and several fields are an error. A signature image placed
            with ``stamp`` stays visible.
        reason: Why the document is signed. Shown by viewers.
        location: Where it was signed.
        contact: How to reach the signer.

    Raises:
        MissingDependencyError: pyHanko is not installed.
        UnknownFieldError: *field* names no field.
        SigningError: The key could not be loaded, *field* is not a signature field
            or is ambiguous, or pyHanko refused to sign.
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
    signature_fields = [f for f in info.fields if f.kind is FieldKind.SIGNATURE]
    new_field_spec = None
    if field is not None:
        target = _match_field(field, info, {f.name: f for f in info.fields})
        if target.kind is not FieldKind.SIGNATURE:
            raise SigningError(f"{target.name}: is a {target.kind.value} field, only signature fields can be signed")
        name = target.name
    elif len(signature_fields) == 1:
        name = signature_fields[0].name
    elif not signature_fields:
        name = DEFAULT_FIELD
        new_field_spec = fields.SigFieldSpec(sig_field_name=name)
    else:
        raise SigningError(
            "The document has several signature fields, pick one: " + ", ".join(f.name for f in signature_fields)
        )

    style: Any = None
    if new_field_spec is None:
        widgets = objects[name][1]
        if any(widget.get("/AP") is not None for widget in widgets):
            style = _KeepAppearance()

    metadata = signers.PdfSignatureMetadata(field_name=name, reason=reason, location=location, contact_info=contact)
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
