from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from pdfform.cli import main_cli


@pytest.fixture
def run(tmp_path):
    runner = CliRunner()

    def _run(*args, **kwargs):
        return runner.invoke(main_cli, [str(a) for a in args], **kwargs)

    return _run


@pytest.fixture
def hybrid_path(tmp_path, hybrid_bytes):
    path = tmp_path / "hybrid.pdf"
    path.write_bytes(hybrid_bytes)
    return path


@pytest.fixture
def dynamic_path(tmp_path, dynamic_bytes):
    path = tmp_path / "dynamic.pdf"
    path.write_bytes(dynamic_bytes)
    return path


def test_schema_prints_json(run, form_path):
    result = run("schema", form_path)
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["properties"]["Consent"]["type"] == "boolean"


def test_schema_writes_a_file(run, form_path, tmp_path):
    out = tmp_path / "schema.json"
    assert run("schema", form_path, "-o", out).exit_code == 0
    assert json.loads(out.read_text())["type"] == "object"


def test_schema_nested(run, form_path):
    schema = json.loads(run("schema", form_path, "--nested").stdout)
    assert "Address" in schema["properties"]


def test_schema_of_a_dynamic_xfa_form_fails_cleanly(run, dynamic_path):
    result = run("schema", dynamic_path)
    assert result.exit_code == 2
    assert "dynamic XFA" in result.output


def test_fields_table(run, form_path):
    result = run("fields", form_path)
    assert result.exit_code == 0
    assert "Consent" in result.stdout
    assert "on=Ja" in result.stdout
    assert "Submit" not in result.stdout


def test_fields_all_includes_buttons_and_read_only(run, form_path):
    assert "Submit" in run("fields", form_path, "--all").stdout


def test_fields_json(run, form_path):
    payload = json.loads(run("fields", form_path, "--json").stdout)
    consent = next(f for f in payload if f["name"] == "Consent")
    assert consent["options"] == ["Ja"]


def test_values_of_a_blank_form_is_empty(run, form_path):
    assert json.loads(run("values", form_path).stdout) == {}


def test_values_include_empty(run, form_path):
    values = json.loads(run("values", form_path, "--include-empty").stdout)
    assert values["Consent"] is False


def test_fill_with_set_options(run, form_path, tmp_path):
    out = tmp_path / "filled.pdf"
    result = run("fill", form_path, "--set", "Name=Erika", "--set", "Consent=true", "-o", out)
    assert result.exit_code == 0
    assert json.loads(run("values", out).stdout) == {"Name": "Erika", "Consent": True}


def test_fill_from_a_json_file(run, form_path, tmp_path):
    data = tmp_path / "values.json"
    data.write_text(json.dumps({"Name": "Erika", "Colour": "Blue"}))
    out = tmp_path / "filled.pdf"
    assert run("fill", form_path, "-d", data, "-o", out).exit_code == 0
    assert json.loads(run("values", out).stdout)["Colour"] == "Blue"


def test_fill_from_stdin(run, form_path, tmp_path):
    out = tmp_path / "filled.pdf"
    result = run("fill", form_path, "-d", "-", "-o", out, input=json.dumps({"Name": "Erika"}))
    assert result.exit_code == 0
    assert json.loads(run("values", out).stdout)["Name"] == "Erika"


def test_set_overrides_the_file(run, form_path, tmp_path):
    data = tmp_path / "values.json"
    data.write_text(json.dumps({"Name": "From file"}))
    out = tmp_path / "filled.pdf"
    run("fill", form_path, "-d", data, "--set", "Name=From flag", "-o", out)
    assert json.loads(run("values", out).stdout)["Name"] == "From flag"


def test_fill_needs_some_values(run, form_path, tmp_path):
    result = run("fill", form_path, "-o", tmp_path / "out.pdf")
    assert result.exit_code != 0
    assert "No values given" in result.output


def test_set_requires_an_equals_sign(run, form_path, tmp_path):
    result = run("fill", form_path, "--set", "Name", "-o", tmp_path / "out.pdf")
    assert result.exit_code != 0
    assert "NAME=VALUE" in result.output


def test_fill_reports_a_bad_value(run, form_path, tmp_path):
    result = run("fill", form_path, "--set", "Colour=Green", "-o", tmp_path / "out.pdf")
    assert result.exit_code == 2
    assert "Red, Blue" in result.output


def test_fill_reports_an_unknown_field(run, form_path, tmp_path):
    result = run("fill", form_path, "--set", "Nope=1", "-o", tmp_path / "out.pdf")
    assert result.exit_code == 2
    assert "no field named" in result.output


def test_fill_rejects_malformed_json(run, form_path, tmp_path):
    data = tmp_path / "values.json"
    data.write_text("{not json")
    result = run("fill", form_path, "-d", data, "-o", tmp_path / "out.pdf")
    assert result.exit_code != 0
    assert "Could not parse" in result.output


def test_fill_flatten(run, form_path, tmp_path):
    out = tmp_path / "flat.pdf"
    assert run("fill", form_path, "--set", "Name=Erika", "-o", out, "--flatten").exit_code == 0
    assert json.loads(run("values", out).stdout) == {}


def test_set_parses_booleans_and_null(run, form_path, tmp_path):
    out = tmp_path / "filled.pdf"
    run("fill", form_path, "--set", "Consent=TRUE", "--set", "Name=null", "-o", out)
    values = json.loads(run("values", out).stdout)
    assert values == {"Consent": True}


def test_strip_xfa(run, hybrid_path, tmp_path):
    out = tmp_path / "stripped.pdf"
    assert run("strip-xfa", hybrid_path, "-o", out).exit_code == 0
    assert "xfa" not in run("schema", out).stdout.lower()


def test_strip_xfa_on_a_plain_form_says_so(run, form_path, tmp_path):
    result = run("strip-xfa", form_path, "-o", tmp_path / "out.pdf")
    assert result.exit_code == 0
    assert "No XFA layer" in result.output


def test_xfa_lists_packets(run, hybrid_path):
    result = run("xfa", hybrid_path)
    assert result.exit_code == 0
    assert "template" in result.stdout
    assert "datasets" in result.stdout


def test_xfa_dumps_a_packet(run, hybrid_path):
    assert "xfa-template" in run("xfa", hybrid_path, "--packet", "template").stdout


def test_xfa_rejects_an_unknown_packet(run, hybrid_path):
    result = run("xfa", hybrid_path, "--packet", "nope")
    assert result.exit_code != 0
    assert "No packet named" in result.output


def test_xfa_on_a_plain_form(run, form_path):
    result = run("xfa", form_path)
    assert result.exit_code == 0
    assert "No XFA layer" in result.output


def test_hybrid_forms_warn_on_the_error_stream(run, hybrid_path):
    assert "static XFA layer" in run("schema", hybrid_path).output


def test_help_lists_the_commands(run):
    output = run("--help").output
    for command in ("schema", "fields", "values", "fill", "strip-xfa", "xfa"):
        assert command in output


def test_validate_passes_on_good_values(run, form_path, tmp_path):
    data = tmp_path / "values.json"
    data.write_text(json.dumps({"Name": "Erika", "Notes": "x"}))
    result = run("validate", form_path, "-d", data)
    assert result.exit_code == 0
    assert "validate against the derived schema" in result.output


def test_validate_reports_problems_and_fails(run, form_path, tmp_path):
    data = tmp_path / "values.json"
    data.write_text(json.dumps({"Notes": "x", "Colour": "Green"}))
    result = run("validate", form_path, "-d", data)
    assert result.exit_code == 1
    assert "Colour:" in result.output


def test_fill_validate_writes_nothing_on_a_bad_value(run, form_path, tmp_path):
    out = tmp_path / "filled.pdf"
    data = tmp_path / "values.json"
    data.write_text(json.dumps({"Notes": "x", "Colour": "Green"}))
    result = run("fill", form_path, "-d", data, "-o", out, "--validate")
    assert result.exit_code != 0
    assert not out.exists()


def test_fill_validate_allows_good_values(run, form_path, tmp_path):
    out = tmp_path / "filled.pdf"
    result = run("fill", form_path, "--set", "Name=Erika", "--set", "Notes=x", "-o", out, "--validate")
    assert result.exit_code == 0
    assert out.exists()


def test_fill_without_validate_stays_permissive(run, form_path, tmp_path):
    """A check box takes "yes", which the strict schema would reject."""
    out = tmp_path / "filled.pdf"
    assert run("fill", form_path, "--set", "Consent=yes", "-o", out).exit_code == 0
    assert json.loads(run("values", out).stdout)["Consent"] is True
