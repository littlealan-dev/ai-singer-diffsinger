# Backend validation fixtures

This runbook describes the canonical local bootstrap path for backend and pipeline validation when a checkout does not include large score and voicebank fixtures.

## Goal

Use a predictable local setup so backend-focused suites can move from asset-light smoke coverage to deeper MusicXML + voicebank validation without changing application code.

## Baseline environment

Create a local virtualenv and install the Python test dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt pytest
```

## Fixture layout

The deeper backend suites expect local assets under `assets/`.

Minimum score fixtures:

```text
assets/test_data/amazing-grace-satb-verse1.xml
assets/test_data/o-holy-night.xml
```

Minimum voicebank fixtures:

```text
assets/voicebanks/Raine_Rena_2.01/
```

Useful optional fixtures for broader voicebank coverage:

```text
assets/voicebanks/Katyusha_v170/configs/
assets/test_data/amazing-grace-satb-zipped.mxl
assets/test_data/my-tribute-bars19-36.xml
```

## Preferred bootstrap path

If you already have a workstation checkout with the private assets populated, mirror them into this repo with either a symlink or a direct copy.

Symlink approach:

```bash
ln -s /path/to/asset-source/assets ./assets
```

Copy approach:

```bash
mkdir -p assets
cp -R /path/to/asset-source/assets/test_data assets/
cp -R /path/to/asset-source/assets/voicebanks assets/
```

Use the symlink approach when you want to keep multiple backend branches aligned to one shared fixture source.

## Validation tiers

Asset-light backend slice:

```bash
.venv/bin/python -m pytest -q \
  tests/test_backend_*.py \
  tests/test_pipeline_steps.py \
  tests/test_api.py \
  tests/test_mcp_server.py \
  tests/test_llm_prompt.py
```

This slice is the default checkpoint for `SIGA-4`. It exercises backend and API regressions while allowing fixture-dependent tests to self-skip in lean workspaces.

Deeper fixture-backed backend/integration slice:

```bash
.venv/bin/python -m pytest -q \
  tests/test_backend_integration.py \
  tests/test_backend_e2e_gemini.py \
  tests/test_prompted_workflow_integration.py \
  tests/test_end_to_end.py
```

Run the deeper slice only after the required local `assets/` tree is present.

## Expected behavior without fixtures

- `tests/test_api.py` now self-skips filesystem-dependent score and voicebank checks when local fixtures are missing.
- Broader integration and end-to-end suites may also skip when voicebanks, score files, or external provider prerequisites are absent.
- These skips are expected in lean workspaces and should not be treated as backend regressions by themselves.

## When to use this runbook

- New backend checkouts created without private fixtures
- CI-like local validation where only backend code changes are under review
- CTO review checkpoints that need a reproducible explanation for skipped integration coverage
