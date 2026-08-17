"""Command line interface for :mod:`pdffill`."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from pdffill.extract import extract_form, get_acroform, open_pdf
from pdffill.fill import fill_form, flatten_values, strip_xfa_layer
from pdffill.model import OFF_STATE, FieldKind, FormInfo, PdfFormError, XfaKind
from pdffill.schema import build_schema, current_values, validate_values
from pdffill.xfa import xfa_packets

logger = logging.getLogger("pdffill")

PDF_ARG = click.Path(exists=True, dir_okay=False, readable=True, path_type=Path)
OUT_OPT = click.Path(dir_okay=False, writable=True, path_type=Path)


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING - min(verbose, 2) * 10
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def _emit(payload: Any, output: Path | None, indent: int) -> None:
    text = json.dumps(payload, indent=indent or None, ensure_ascii=False, sort_keys=False)
    if output is None:
        click.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        click.echo(f"Wrote {output}", err=True)


def _warn_xfa(info: FormInfo) -> None:
    if info.xfa is XfaKind.HYBRID:
        click.echo(
            "warning: this form carries a static XFA layer. Acrobat may prefer the XFA "
            "data over the values written here; `pdffill fill` drops that layer by default.",
            err=True,
        )


class _FormError(click.ClickException):
    """A library error, reported without a traceback."""

    exit_code = 2


class _Group(click.Group):
    """Turns :class:`PdfFormError` into a clean message instead of a traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except PdfFormError as exc:
            raise _FormError(str(exc)) from exc


@click.group(cls=_Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="pdffill")
@click.option("-v", "--verbose", count=True, help="Increase log verbosity. Repeatable.")
def main_cli(verbose: int) -> None:
    """Inspect, describe and fill PDF forms.

    \b
    Typical session:
      pdffill fields form.pdf                     # see what is in there
      pdffill schema form.pdf -o schema.json      # derive a JSON Schema
      pdffill values form.pdf -o data.json        # start from the current values
      pdffill fill form.pdf -d data.json -o out.pdf
    """
    _configure_logging(verbose)


@main_cli.command()
@click.argument("pdf", type=PDF_ARG)
@click.option("-o", "--output", type=OUT_OPT, help="Write the schema here instead of to stdout.")
@click.option("--nested/--flat", default=False, help="Split dot-separated field names into nested objects.")
@click.option("--include-read-only", is_flag=True, help="Also describe read-only fields.")
@click.option("--include-signatures", is_flag=True, help="Also describe signature fields.")
@click.option(
    "--infer-labels",
    is_flag=True,
    help="Guess a description for fields without a tooltip from the text next to them.",
)
@click.option("--indent", type=int, default=2, show_default=True, help="JSON indentation; 0 for one line.")
def schema(
    pdf: Path,
    output: Path | None,
    nested: bool,
    include_read_only: bool,
    include_signatures: bool,
    infer_labels: bool,
    indent: int,
) -> None:
    """Derive a JSON Schema from the form fields of PDF."""
    info = extract_form(pdf, infer_labels=infer_labels)
    _warn_xfa(info)
    document = build_schema(
        info,
        nested=nested,
        include_read_only=include_read_only,
        include_signatures=include_signatures,
    )
    _emit(document, output, indent)


@main_cli.command()
@click.argument("pdf", type=PDF_ARG)
@click.option("--json", "as_json", is_flag=True, help="Emit the inventory as JSON instead of a table.")
@click.option("--infer-labels", is_flag=True, help="Guess a label for each field from the text next to it.")
@click.option("--all", "show_all", is_flag=True, help="Include push buttons and read-only fields.")
def fields(pdf: Path, as_json: bool, infer_labels: bool, show_all: bool) -> None:
    """List the form fields of PDF with their type, options and current value."""
    info = extract_form(pdf, infer_labels=infer_labels)
    _warn_xfa(info)
    selected = [f for f in info.fields if show_all or (f.kind.holds_value and not f.read_only)]

    if as_json:
        payload = [
            {
                "name": f.name,
                "kind": f.kind.value,
                "pages": f.pages,
                "value": f.value,
                "options": f.options or f.on_states,
                "optionLabels": f.option_labels,
                "maxLength": f.max_length,
                "required": f.required,
                "readOnly": f.read_only,
                "description": f.describe(),
            }
            for f in selected
        ]
        _emit(payload, None, 2)
        return

    if not selected:
        click.echo("No fillable form fields found.", err=True)
        if info.xfa is XfaKind.DYNAMIC:
            click.echo("This is a dynamic XFA form; there is no static field list.", err=True)
        return

    name_width = min(max(len(f.name) for f in selected), 52)
    click.echo(f"{'FIELD'.ljust(name_width)}  {'KIND'.ljust(10)}  PG  DETAIL")
    for field in selected:
        detail_parts: list[str] = []
        if field.kind is FieldKind.CHECKBOX:
            detail_parts.append(f"on={field.on_states[0] if field.on_states else 'Yes'}")
        options = field.options or field.on_states
        if options:
            detail_parts.append("[" + ", ".join(options[:6]) + ("…" if len(options) > 6 else "") + "]")
        if field.max_length:
            detail_parts.append(f"max {field.max_length}")
        if field.required:
            detail_parts.append("required")
        if field.read_only:
            detail_parts.append("read-only")
        if field.value and field.value != OFF_STATE:
            detail_parts.append(f"= {field.value!r}")
        described = field.describe()
        if described:
            detail_parts.append(f"“{described}”")
        pages = ",".join(str(p + 1) for p in field.pages) or "-"
        click.echo(
            f"{_clip(field.name, name_width).ljust(name_width)}  {field.kind.value.ljust(10)}  {pages.rjust(2)}  "
            + " ".join(detail_parts)
        )
    click.echo(f"\n{len(selected)} field(s) over {info.n_pages} page(s).", err=True)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


@main_cli.command()
@click.argument("pdf", type=PDF_ARG)
@click.option("-o", "--output", type=OUT_OPT, help="Write the values here instead of to stdout.")
@click.option("--include-empty", is_flag=True, help="Also emit fields that are currently empty.")
@click.option("--include-read-only", is_flag=True, help="Also emit read-only fields.")
@click.option("--indent", type=int, default=2, show_default=True, help="JSON indentation; 0 for one line.")
def values(pdf: Path, output: Path | None, include_empty: bool, include_read_only: bool, indent: int) -> None:
    """Dump the values currently stored in PDF as JSON.

    The result is accepted by `pdffill fill`, so this is the quickest way to get
    a template to edit.
    """
    info = extract_form(pdf)
    payload = current_values(info, include_empty=include_empty, include_read_only=include_read_only)
    _emit(payload, output, indent)


@main_cli.command()
@click.argument("pdf", type=PDF_ARG)
@click.option(
    "-d",
    "--data",
    type=click.Path(dir_okay=False, allow_dash=True, path_type=Path),
    help="JSON file with the field values. Use - to read from stdin.",
)
@click.option("--set", "assignments", multiple=True, metavar="NAME=VALUE", help="Set one field. Repeatable.")
@click.option("-o", "--output", type=OUT_OPT, required=True, help="Where to write the filled PDF.")
@click.option("--flatten", is_flag=True, help="Bake the values into the page and drop the form.")
@click.option(
    "--strict/--no-strict",
    default=True,
    show_default=True,
    help="Fail on unknown field names and values that do not fit their field.",
)
@click.option(
    "--need-appearances/--no-need-appearances",
    default=True,
    show_default=True,
    help="Ask viewers to regenerate the field appearances.",
)
@click.option(
    "--strip-xfa/--keep-xfa",
    "strip_xfa",
    default=None,
    help="Drop the XFA layer. The default drops it only for static XFA forms.",
)
@click.option("--validate", "check", is_flag=True, help="Validate the values against the derived schema first.")
def fill(
    pdf: Path,
    data: Path | None,
    assignments: tuple[str, ...],
    output: Path,
    flatten: bool,
    strict: bool,
    need_appearances: bool,
    strip_xfa: bool | None,
    check: bool,
) -> None:
    """Fill the form in PDF and write the result to --output.

    \b
    Values come from a JSON file, from --set, or both; --set wins.
      pdffill fill form.pdf --set Name=Doe --set Agreed=true -o out.pdf
      pdffill fill form.pdf -d values.json -o out.pdf
      cat values.json | pdffill fill form.pdf -d - -o out.pdf
    """
    payload: dict[str, Any] = _load_values(data) if data is not None else {}
    for assignment in assignments:
        name, separator, value = assignment.partition("=")
        if not separator:
            raise click.ClickException(f"--set expects NAME=VALUE, got {assignment!r}")
        payload[name] = _parse_scalar(value)

    if not payload:
        raise click.ClickException("No values given. Use --data and/or --set.")

    if check:
        problems = validate_values(build_schema(extract_form(pdf)), flatten_values(payload))
        if problems:
            for problem in problems:
                click.echo(problem, err=True)
            raise click.ClickException(f"{len(problems)} value(s) do not match the schema; nothing was written.")

    fill_form(
        pdf,
        payload,
        output,
        flatten=flatten,
        need_appearances=need_appearances,
        strict=strict,
        strip_xfa=strip_xfa,
    )
    click.echo(f"Wrote {output}", err=True)


def _parse_scalar(text: str) -> Any:
    """Interpret a --set value, keeping anything unrecognised as a string."""
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    return text


@main_cli.command()
@click.argument("pdf", type=PDF_ARG)
@click.option(
    "-d",
    "--data",
    required=True,
    type=click.Path(dir_okay=False, allow_dash=True, path_type=Path),
    help="JSON file with the field values. Use - to read from stdin.",
)
def validate(pdf: Path, data: Path) -> None:
    """Check a values file against the schema derived from PDF.

    Reports unknown field names, values outside an enum, strings over
    `maxLength` and missing required fields, then exits non-zero if anything is
    wrong. Filling is more permissive than this on purpose, so a clean run here
    is a stronger guarantee than a successful fill.
    """
    payload = _load_values(data)
    schema = build_schema(extract_form(pdf))
    problems = validate_values(schema, flatten_values(payload))
    if not problems:
        click.echo(f"{len(payload)} value(s) validate against the derived schema.", err=True)
        return
    for problem in problems:
        click.echo(problem, err=True)
    raise click.exceptions.Exit(1)


def _load_values(data: Path) -> dict[str, Any]:
    raw = sys.stdin.read() if str(data) == "-" else data.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Could not parse the values as JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("The values file must contain a JSON object.")
    return payload


@main_cli.command("strip-xfa")
@click.argument("pdf", type=PDF_ARG)
@click.option("-o", "--output", type=OUT_OPT, required=True, help="Where to write the result.")
def strip_xfa_command(pdf: Path, output: Path) -> None:
    """Remove the XFA layer of PDF, leaving a plain AcroForm.

    The equivalent of `pdftk in.pdf output out.pdf drop_xfa`. Useful for static
    XFA forms, whose XFA data would otherwise override the AcroForm values.
    """
    info = extract_form(pdf)
    if info.xfa is XfaKind.NONE:
        click.echo("No XFA layer to remove; copying the document unchanged.", err=True)
    elif info.xfa is XfaKind.DYNAMIC:
        click.echo(
            "warning: this is a dynamic XFA form. Removing the XFA layer leaves an empty "
            "document, because the AcroForm side is only a stub.",
            err=True,
        )
    strip_xfa_layer(pdf, output)
    click.echo(f"Wrote {output}", err=True)


@main_cli.command()
@click.argument("pdf", type=PDF_ARG)
@click.option("--packet", help="Dump this XFA packet, for example template or datasets.")
@click.option("-o", "--output", type=OUT_OPT, help="Write the packet here instead of to stdout.")
def xfa(pdf: Path, packet: str | None, output: Path | None) -> None:
    """Inspect the XFA layer of PDF.

    Without --packet, lists the packets and their sizes. The `template` packet
    holds the real form definition, `datasets` holds the values.
    """
    packets = xfa_packets(get_acroform(open_pdf(pdf)))
    if not packets:
        click.echo("No XFA layer in this document.", err=True)
        return
    if packet is None:
        for name, blob in packets.items():
            click.echo(f"{name:<12} {len(blob):>9} bytes")
        return
    if packet not in packets:
        raise click.ClickException(f"No packet named {packet!r}. Available: " + ", ".join(packets))
    blob = packets[packet]
    if output is None:
        click.echo(blob.decode("utf-8", errors="replace"))
    else:
        output.write_bytes(blob)
        click.echo(f"Wrote {output}", err=True)


if __name__ == "__main__":  # pragma: no cover
    main_cli()
