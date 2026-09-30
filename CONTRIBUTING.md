# Contributing to `pdfform`

## Development environment

The project uses [`uv`](https://docs.astral.sh/uv/) for dependency management.

### Initial setup

```bash
# Create the conda env (provides Python + uv)
micromamba env create -f environment-dev.yml
micromamba activate pdfform

# Install all dependency groups (runtime + dev + test + lint) into a uv-managed venv
uv sync --all-groups
```

All subsequent commands assume the env is activated and `uv` is on `PATH`.

## Running tasks

The project standardises tasks through [`tox`](https://tox.wiki/) with the `tox-uv` runner. Run any environment with:

```bash
uv run tox -e <env>
```

Available environments (defined in `pyproject.toml`):

| Env             | Purpose                                  |
|-----------------|------------------------------------------|
| `format-check`  | `ruff format --check .`                  |
| `lints`         | `ruff check .`                           |
| `typecheck`     | `mypy src/pdfform`                       |
| `py3.12`        | Run pytest under Python 3.12             |
| `py3.14`        | Run pytest under Python 3.14             |

Run the full matrix CI runs with:

```bash
uv run tox
```

### Quick commands

```bash
# Format code
uv run ruff format .

# Lint with autofix
uv run ruff check --fix .

# Run tests directly (single Python)
uv run pytest

# Run a single test
uv run pytest tests/test_fill.py::test_radio_can_be_cleared -x
```

## Test fixtures

The test forms are built by hand in `tests/formbuilder.py` from `pypdf` primitives rather than with a form generator. That is deliberate: the cases worth testing are the ones a friendly generator smooths over, such as a check box whose on-state is `/Ja`, a radio group whose kids each carry a different on-state, a text field whose `/AP` `/N` is a stream and not a state dictionary, and hierarchical field names.

`tests/formbuilder.py` is on the test `pythonpath`, so test modules import from it directly. Fixtures wrapping it live in `tests/conftest.py`.

When adding support for a construct, add it to `build_form` rather than creating a one-off document, so the whole suite exercises it.

## Releases

Versioning and tagging are automated by [release-please](https://github.com/googleapis/release-please) (`.github/workflows/release-please.yml`). Publishing is handled by `.github/workflows/publish.yml`:

- release-please watches commits on `main` and opens/maintains a release PR that bumps `pyproject.toml` and updates `CHANGELOG.md`.
- Merging the release PR cuts a `vX.Y.Z` tag and a GitHub release.
- Publishing to PyPI is opt-in: it only runs when the repository variable `PYPI_PUBLISH` is set to `true`, and it uses trusted publishing (OIDC). It can also be triggered by hand from the Actions tab.

Use [Conventional Commits](https://www.conventionalcommits.org/) on `main` so release-please can pick the next version (`fix:` → patch, `feat:` → minor, `feat!:` / `BREAKING CHANGE:` → major).
