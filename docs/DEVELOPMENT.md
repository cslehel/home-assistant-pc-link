# Development

## Service (runs on any OS)

```bash
pip install -r requirements-dev.txt
pytest -q
```

All Windows backends are injected, so the orchestrator, time accounting,
rules, presence, the local API and the entity catalog are tested with
in-memory fakes on Linux/macOS as well.

## Home Assistant integration

The integration lives in `custom_components/pclink/` and is tested with
Home Assistant's own test harness
([pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)).
Home Assistant releases monthly and each release pins its Python version, so
the tests run against **two** cores:

| | Python | Install |
|---|---|---|
| newest release | 3.13 | `pip install pytest-homeassistant-custom-component PyTurboJPEG tzdata` |
| oldest supported (2024.3) | 3.11 | `pip install pytest-homeassistant-custom-component==0.13.109 zeroconf==0.131.0 PyTurboJPEG tzdata` |

Use a separate virtual environment per core (they pin conflicting versions),
then from the repository root:

```bash
pytest tests_ha -q -p no:cacheprovider
```

`PyTurboJPEG` and `tzdata` are pulled in by Home Assistant's camera component
and the harness's time-zone fixture respectively; neither is needed at runtime.

The GitHub Actions workflow (`.github/workflows/test.yml`) runs both suites on
every push and again every Monday, so a new Home Assistant release that breaks
something shows up within a week even without code changes. It also runs
`hassfest` (manifest validation) and the HACS validation action.

## Regenerating the integration's translations

`custom_components/pclink/strings.json` and `translations/en.json` are
generated from `scripts/gen_ha_strings.py`, which also checks that every
field in `services.yaml` has a translation:

```bash
python scripts/gen_ha_strings.py .
```

## Keeping the example config in sync

`config.example.yaml` is embedded in `pclink/example_config.py` for the
`init-config` command; after editing it run:

```bash
python scripts/sync_example_config.py
```

## Releasing

Bump the version in `pclink/__init__.py`, `pyproject.toml` and
`custom_components/pclink/manifest.json` together and tag the release; HACS
picks the integration up from the tag.
