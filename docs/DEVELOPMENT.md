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

Home Assistant does not support Windows, but the suite can be run there for a
quick check: put two stub modules named `fcntl` and `resource` (no-op
`flock` / `getrlimit` functions) into the test environment's `site-packages`,
and load a small pytest plugin that turns `pytest_socket.disable_socket` into
a no-op (Windows event loops need a socket pair that the harness blocks). CI on
Linux remains the reference.

Both cores install on Windows. For the oldest one, which needs Python 3.11 or
3.12, [uv](https://docs.astral.sh/uv/) fetches the interpreter (no system-wide
install needed); from PowerShell, with `<stubs>` the folder holding
`fcntl.py`, `resource.py` and the plugin (`winsock_plugin.py`, containing
`import pytest_socket; pytest_socket.disable_socket = lambda *a, **k: None`):

```powershell
python -m pip install uv --target $env:TEMP\uvpkg
& $env:TEMP\uvpkg\bin\uv.exe python install 3.12
& $env:TEMP\uvpkg\bin\uv.exe venv $env:TEMP\ha2024 --python 3.12
& $env:TEMP\uvpkg\bin\uv.exe pip install --python $env:TEMP\ha2024\Scripts\python.exe `
    "pytest-homeassistant-custom-component==0.13.109" "zeroconf==0.131.0" PyTurboJPEG tzdata
$env:PYTHONPATH = "<stubs>"
& $env:TEMP\ha2024\Scripts\python.exe -m pytest tests_ha -q -p winsock_plugin -p no:cacheprovider
```

`0.13.109` is the harness release for Home Assistant 2024.3 (it installs
2024.3.3); the install takes a few minutes. Two differences of that
release matter to the tests: `hass.async_block_till_done()` does not wait for
background tasks (and they do not start eagerly), and while
`async_remove_entry` runs the entry being deleted is still listed by
`hass.config_entries.async_entries()`. `eager_start=True` only takes effect on
Python 3.12 and later, so a local run on 3.12 can pass where CI, which runs
this release on 3.11, fails: tests that look at what a background task sent
wait for it with `settle()` from `tests_ha/test_rollup.py`.

The service tests run on Linux in CI, and some behaviour differs from Windows:
`socket.socketpair()` there is a Unix socket holding only about 200 KB, so a
test that sends more than that before reading must send from a thread.

The integration must keep working on the oldest supported release and on the
newest one. Two places adapt to the version at run time: the coordinator
passes `config_entry` only when `DataUpdateCoordinator` accepts it, and
`const.device_entry_ids()` reads a device's config entry from whichever
attribute the release provides.

The "all PCs" roll-up (`rollup.py`) is not tied to one PC: `build_rollup()` is
a pure function, a hub in `hass.data` listens to every coordinator, and the
entities are added by the platforms of one config entry (the first set up
while two PCs are configured). The hub also sends PCs with a shared budget
the usage of the other PCs. `tests_ha/test_rollup.py` covers the sums, the
entities, the hand-over when that entry is deleted and what is sent to whom.

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

The device-trigger types and their labels are taken from `USER_TRIGGERS` /
`PC_TRIGGERS` in `pclink/entities.py`; the script also rewrites the two lists
in `custom_components/pclink/const.py` when they differ. Run it after adding
an event there.

## Keeping the example config in sync

`config.example.yaml` is embedded in `pclink/example_config.py` for the
`init-config` command; after editing it run:

```bash
python scripts/sync_example_config.py
```

## Building the standalone Windows version

GitHub Actions builds it on every push (`.github/workflows/build.yml`) and
tests the resulting zip on a real Windows machine: it runs `selftest` to prove
every bundled backend loads, installs the service with `install.ps1`, checks it
is running with the hardened permissions, then uninstalls it. The zip is
attached to the workflow run as an artifact.

The build installs exactly the packages listed, with their hashes, in
`packaging/requirements-lock.txt` (`pip install --require-hashes`). To take
newer versions run `python packaging/make_lock.py`, look at the difference and
commit it; the lock is made for the Python version the workflow builds with
(`PYTHON_VERSION` in the script must match `python-version` in `build.yml`).
The build also writes a `VERSION` file into the package; the self-update
refuses a download whose `VERSION` is not the release's tag or is not newer
than the installed version.

Signing happens in a separate job (`package`) that only runs on the release
path, in the GitHub environment `release-signing`, and runs no code from the
repository or from PyPI. Put the certificate secrets
(`SIGN_PFX_BASE64` / `SIGN_PFX_PASSWORD`) in that environment and give it
required reviewers. With them configured the job signs both executables
and the PowerShell scripts (`install.ps1`, `uninstall.ps1`,
`defender-exclusion.ps1`), and then makes and signs `pclink.cat`, a catalog
of every file in the package (`New-FileCatalog`). The self-update of a signed
copy needs all of it (`pclink/codesign.py`: both executables, `install.ps1`
and the catalog), so a signed build made any other way must do the same:
sign the executables and `install.ps1`, create the catalog last, over the
finished package folder, sign it, and do not change any file afterwards
(signing a script changes it, so it has to come before the catalog).

To build locally on Windows:

```powershell
pip install -r packaging/requirements-build.txt
python packaging/make_icon.py
pyinstaller --noconfirm --clean packaging/pclink.spec
dist\pclink\pclink.exe selftest
```

Then copy `install.ps1`, `install.cmd`, `uninstall.ps1`, `uninstall.cmd`,
`README-FIRST.txt` and `defender-exclusion.ps1` from `packaging/`, plus
`config.example.yaml` and `LICENSE`, into `dist\pclink`, and zip that folder
as `PCLink\` inside `PCLink-<version>-windows-x64.zip` - the name and layout
the self-update looks for in a release. The spec bundles `tzdata` (the time
zone database for `time_limits.time_zone`).

## Icons

`packaging/make_icon.py` draws the icon (a monitor showing a clock on a green
tile) with the standard library only. Without arguments it writes
`packaging/pclink.ico` for the executables (16 to 256 px; generated at build
time, not committed). The integration's brand images are committed; regenerate
them after changing the drawing:

```bash
python packaging/make_icon.py --brand custom_components/pclink/brand
```

`packaging/entry_pclink.py` and `packaging/entry_agent.py` are the entry
scripts; PyInstaller runs entry scripts as top-level code, so they must use
absolute imports. In the build, `pclink.runtime.is_frozen()` is true: the
service registers `pclink.exe service` as its binary and starts agents as
`pclink-agent.exe`.

## Tests worth knowing about

- `tests/test_review_fixes.py` holds regression tests for defects found in a
  full review: PIN gating, command results, configuration validation, the IPC
  line reader, a stalled WebSocket client, the usage report, the block list
  and the idle / away counters.
- `tests/test_updater.py` covers version comparison, release selection, the
  download checks and the archive extraction of the self-update without
  touching the network.
- `tests/test_ideas.py` covers the fixed time zone, rules edited from Home
  Assistant (including what counts as loosening), weekly budgets per rule,
  the read-only API token, the display switch, the signature policy of an
  update and shared budgets fed by Home Assistant. One test reads real signatures with PowerShell and runs on Windows
  only.
- `tests/test_review3_real.py` exercises the security-relevant parts for real,
  without administrator rights: the API's exclusive listeners on both loopback
  (or both wildcard) addresses and a squatter on `[::1]`, the agent / service
  handshake over real sockets (wrong token, an impostor on the port, the old
  hello), the agent's watch on its service with real processes, Authenticode
  signatures and a catalog read with PowerShell (including a path with
  non-ASCII characters), the network adapters `GetAdaptersAddresses` reports,
  and a data folder with a real junction and hard link (`mklink`). The
  Windows-only tests are skipped elsewhere; the whole file takes about ten
  seconds.
- The parts that need administrator rights or real hardware are not covered by
  unit tests: installing the service, the data-folder permissions, logon
  blocking, the tray, UI Automation, Core Audio, switching a display off and
  on, and installing an update that is really signed (the signing step of the
  build workflow only runs with a certificate). The build workflow installs
  the service on a real Windows machine; the rest is checked by hand.

## Releasing

1. Bump the version in `pclink/__init__.py`, `pyproject.toml` and
   `custom_components/pclink/manifest.json` together, and add a section for it
   to `CHANGELOG.md` (the tests fail when the three numbers differ or the
   section is missing). Merge to `main`.
2. Publish, in any of these ways:
   - GitHub: **Actions** tab, **Release** workflow, **Run workflow** on `main`.
     It creates the tag `vX.Y.Z` itself.
   - Push a tag: `git tag -a vX.Y.Z -m "PC Link X.Y.Z" && git push origin vX.Y.Z`.
   - Create the release by hand on GitHub with a new `vX.Y.Z` tag; the
     workflow then attaches the zip to it.

The workflow (`.github/workflows/release.yml`) builds and tests the Windows
zip, checks the tag against all three version numbers, and publishes the
release with the zip attached and the changelog section as its notes. HACS
installs the integration from the release tag.
