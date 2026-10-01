# pdfform

Derive a JSON Schema from a PDF form, then fill the form from JSON. Usable as a command line tool or as a library.

There is no single library that reads an AcroForm, describes it as a schema, and writes values back correctly. `pypdf` and `pikepdf` give you the raw dictionaries, `pdf-lib` gives you a typed field API in JavaScript, and the mapping to JSON Schema is left to you, along with the handful of details that decide whether a filled form actually shows anything in a viewer. `pdfform` is that missing layer.

## Features

- **Schema extraction.** One JSON Schema property per fillable field, with types, enums, `maxLength`, defaults and descriptions.
- **Filling** from a JSON file, from `--set NAME=VALUE`, or from a Python dict.
- **Correct button handling.** Check box on-states are read per widget, not assumed to be `Yes`, and both `/V` and `/AS` are written.
- **Flattening** that bakes the appearance streams into the page content using the placement algorithm from the PDF spec, rather than only translating them.
- **XFA awareness.** Static and dynamic XFA are told apart, static XFA layers can be dropped (the `pdftk drop_xfa` equivalent), and dynamic XFA fails loudly instead of producing an empty schema.
- **Signature images.** `stamp` draws a PDF, PNG, JPEG or SVG signature into a signature field.
- **Cryptographic signing.** `sign` signs the document with a certificate, as a separate step.
- **Label inference.** Optional best-effort guessing of what a field called `Text12` actually means, from the text printed next to it.

## Installation

Requires Python >= 3.12.

```bash
pip install pdfform
```

The signature commands bring their own dependencies, so the base install stays small:

```bash
pip install 'pdfform[stamp]'   # PNG, JPEG and SVG signatures (Pillow, svglib). PDF signatures need nothing extra
pip install 'pdfform[sign]'    # cryptographic signatures (pyHanko)
```

From a checkout, `uv sync --all-groups --all-extras` sets everything up; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Command line

```bash
# What is in this form?
pdfform fields form.pdf

# Derive a JSON Schema
pdfform schema form.pdf -o schema.json

# Dump the current values as a starting point, edit, then write them back
pdfform values form.pdf --include-empty -o data.json
pdfform fill form.pdf -d data.json -o filled.pdf

# Or set a few fields directly
pdfform fill form.pdf --set Name=Doe --set Agreed=true --set Colour=Blue -o filled.pdf

# Produce a non-editable document
pdfform fill form.pdf -d data.json -o filled.pdf --flatten
```

`pdfform fields` prints one line per field with its kind, page, options, current value and description:

```
FIELD                        KIND        PG  DETAIL
Haushaltsjahr                text         1  max 4 “Haushaltsjahr”
Grund_Probandenentgelt       checkbox     1  on=Ja [Ja] “Probandenentgelt”
Mitarbeiter_des_Klinikums    radio        1  [Nein, Ja] “Ist der Zahlungsempfänger Mitarbeiter des Klinikums?”
Antragsteller_Unterschrift   signature    2  “Unterschrift des Antragstellers”
```

Data can be checked against the derived schema before anything is written:

```bash
pdfform validate form.pdf -d data.json
pdfform fill form.pdf -d data.json -o filled.pdf --validate   # refuses to write on a mismatch
```

Other commands: `pdfform stamp` and `pdfform sign`, described below, `pdfform strip-xfa` removes an XFA layer, `pdfform xfa` lists or dumps the XFA packets. `pdfform --help` covers the rest.

## Library

```python
from pdfform import extract_form, build_schema, fill_form

info = extract_form("form.pdf")
schema = build_schema(info)  # a JSON Schema document

fill_form("form.pdf", {"Name": "Doe", "Agreed": True}, "filled.pdf")
```

`fill_form` returns the resulting bytes, so the `output` argument is optional and fills can be chained. Values may be nested (`{"Address": {"Street": ...}}`) or flat with dot-separated keys (`{"Address.Street": ...}`).

Working with the field inventory directly:

```python
for field in info.fillable:
    print(field.name, field.kind, field.on_states, field.describe())
```

`extract_form` never raises on a document that simply has no form; it returns an empty `FormInfo`. Errors that do get raised (`DynamicXfaError`, `UnknownFieldError`, `FieldValueError`) all derive from `PdfFormError`.

## How fields map to JSON Schema

| Field kind  | JSON Schema |
|-------------|-------------|
| text        | `{"type": "string"}`, plus `maxLength` when `/MaxLen` is set |
| check box   | `{"type": "boolean"}` |
| radio group | `{"type": "string", "enum": [...on states...]}` |
| dropdown    | `{"type": "string", "enum": [...]}`, or a free string for an editable combo box |
| list box    | as dropdown, or an array of them when multi-select is set |
| push button | omitted, it holds no value |
| signature   | omitted by default, it takes no value. See `stamp` and `sign` |

Read-only fields are omitted unless `--include-read-only` is given. Everything needed to write a value back is preserved under the `x-pdf` keyword, which validators ignore:

```json
"Grund_Probandenentgelt": {
  "type": "boolean",
  "description": "Probandenentgelt",
  "x-pdf": {"field": "Grund_Probandenentgelt", "kind": "checkbox", "pages": [0], "onState": "Ja", "offState": "Off"}
}
```

Filling accepts more than the schema strictly describes: a check box takes `true`/`false` as well as its on-state name, a radio group or dropdown takes an option's display label as well as its export value, and `null` clears a field. Unknown names and out-of-range values are errors by default and warnings under `--no-strict`. Use `validate` when the data is meant to be schema-clean, since it holds it to the stricter standard.

### Compared to other tools

`PyPDFForm` also emits a JSON Schema, but it is lossy where it matters: a radio group becomes `{"type": "integer"}` addressed by option index, so the actual export values are unrecoverable, and a check box becomes a bare boolean that discards the on-state. `pdfform` puts the real on-state names in the `enum` and keeps them in `x-pdf`, so a schema plus a filled JSON document is enough to reconstruct exactly what was written. `pdfcpu form export` produces the closest thing on the CLI side but is not JSON Schema, and its checkbox handling only recognises `/Yes` as checked.

## Things that bite when filling PDF forms

These are the reasons this tool exists rather than a forty-line script.

**Check box on-states are not `/Yes`.** The checked value is whichever key appears in the widget's `/AP` `/N` dictionary, and it may be `/On`, `/1`, `/Ja`, `/x` or anything else. `pdfform` reads it per field.

**`/V` alone is not enough.** `/V` is the logical value; `/AS` selects which appearance stream gets drawn. Set only `/V` and the file is correct while the box looks empty in every viewer. For a radio group the rule applies per widget: `/V` goes on the group, every kid gets `/AS`, and only the kid whose own on-state matches is switched on.

**A button value is a name, not a string.** Written as a text string it is silently ignored.

**Appearance streams.** Without regenerating appearances, Acrobat may show a value that Preview, Chrome's viewer or a printer does not. `pdfform` both generates appearance streams for text and choice fields and sets `/NeedAppearances`, since neither is reliable alone.

**`/AP` `/N` is not always a state dictionary.** For text fields it is a single stream, whose dictionary keys are `/BBox`, `/Resources` and friends. Reading those as appearance states is the usual way to invent on-states that do not exist.

**Field names are hierarchical.** The real name is the `/T` of every ancestor joined with `.`, and only the leaf is stored on the field itself. `--nested` splits them back into nested objects. A partial name may itself contain a literal dot, so the flat name stays available under `x-pdf.field`.

**Widgets are usually not the field.** A field with one widget is normally merged into a single dictionary, but a field with several has them as `/Kids`, and the `/AP` sits on the kid. Looking only at the objects returned by a `get_fields()` style call finds no on-state for fields that visibly have check boxes.

**XFA.** Many government and banking forms store the real definition in an XML `/XFA` packet. If AcroForm fields exist alongside it the form is *static* XFA: fillable, but Acrobat may re-read the XFA data on open and discard the values, so `pdfform fill` drops the XFA layer by default. If `/Fields` is empty the form is *dynamic* XFA: script-generated, with no fixed field list, and `pdfform` says so instead of emitting an empty schema. The `template` packet is a much richer schema source than the AcroForm; `pdfform xfa --packet template` dumps it.

**Field names are often meaningless.** `Text12` and `Kontrollkästchen3` produce a schema that is structurally correct and semantically useless. The document's own `/TU` tooltip is used as the description when present. Failing that, `--infer-labels` guesses from the text printed next to each widget, including per-option labels for radio groups. It is a geometric heuristic, right most of the time and wrong some of the time, which is why it is opt-in.

## Flattening

`--flatten` draws each widget's current appearance into the page content stream and removes the form. The appearance is placed by transforming the `/BBox` by the form's `/Matrix`, taking the bounding box of the result, and mapping that onto the annotation `/Rect`, per PDF 32000-1 section 12.5.5. Translating to the rectangle corner instead, which is the common shortcut, misplaces any appearance whose `/BBox` is not at the origin.

Widgets that are hidden, or that have no appearance stream at all, are removed without being painted. That matches what a viewer shows for them.

## Signing

A signature field cannot be filled with a value, and "signing" means two different things. They are two commands, and they compose.

**`stamp` draws a signature image.** It makes the field look signed, nothing more. The image becomes the field's appearance, so `fill --flatten` bakes it into the page like any other widget.

```bash
pdfform stamp form.pdf --field Antragsteller_Unterschrift --image signature.png -o stamped.pdf
```

The source can be a PDF (first page), PNG, JPEG or SVG. The image keeps its aspect ratio and is centred in the field (`--fit contain`), or fills it and distorts (`--fit stretch`). Transparent PNGs keep their transparency. A PDF or SVG stays vector.

**`sign` adds a cryptographic signature.** It needs a certificate and a private key, as a PKCS#12 file or as PEM files, and is written as an incremental update, so earlier signatures stay valid.

```bash
pdfform sign stamped.pdf --p12 me.p12 --field Antragsteller_Unterschrift -o signed.pdf
pdfform sign stamped.pdf --key key.pem --cert cert.pem --ask-passphrase -o signed.pdf
```

The passphrase is never a command line argument, so it stays out of the shell history. Use `--ask-passphrase`, `--passphrase-file`, or the `PDFFORM_PASSPHRASE` environment variable. A form with a single signature field signs that field, a document without one gets an invisible `Signature1`, and several fields need `--field`. An image placed by `stamp` stays visible. Fields that were not signed yet remain signable, so two people can sign two fields one after the other.

**Order: `fill`, then `stamp`, then `sign`.** Anything that rewrites the file afterwards, `fill` and `stamp` included, invalidates the signature. Whether a recipient trusts the signature depends on the certificate: a self-signed one gives a signature that is intact but not trusted. There is no timestamp, since that would need a request to a timestamp authority.

**`fill` does all three at once.** `--stamp FIELD=IMAGE` (repeatable, with `--stamp-fit`) stamps after the values are set. `--sign` signs last, and `--sign-field` picks the field. `fill` takes the same key and passphrase options as `sign`, so the order is always right:

```bash
pdfform fill form.pdf -d data.json --stamp Antragsteller_Unterschrift=signature.png \
  --sign --sign-field Antragsteller_Unterschrift --p12 me.p12 -o signed.pdf
```

With `--flatten` the stamped image is baked into the page before signing. The signature then goes into a new invisible `Signature1`, because flattening removes the form.

In Python:

```python
from pdfform import fill_form, stamp_signature, sign_pdf

stamped = stamp_signature("form.pdf", "Antragsteller_Unterschrift", "signature.png")
sign_pdf(stamped, "signed.pdf", pkcs12="me.p12", passphrase="...", field="Antragsteller_Unterschrift")

# or fill and stamp in one pass, then sign
filled = fill_form("form.pdf", data, stamp={"Antragsteller_Unterschrift": "signature.png"})
sign_pdf(filled, "signed.pdf", pkcs12="me.p12", passphrase="...", field="Antragsteller_Unterschrift")
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
