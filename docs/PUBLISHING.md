# Publishing PC Link to the HACS default list (Home Assistant Marketplace)

A checklist for the maintainer. Since Home Assistant 2026.11, HACS is built into
Home Assistant as **Settings → Marketplace**. Getting listed there still works
the old way: a pull request to [hacs/default](https://github.com/hacs/default).
The Marketplace reads that same list. Publishing documentation:
<https://contributors.home-assistant.io/marketplace/>.

Start only when the release you want to list works on real PCs. For this
project that means Hydra Multi-Seat confirmed on the laptop (seat 2 counted,
locked or logged off when time runs out).

## 1. Check what you are about to publish

- The latest GitHub release is a **full release** (not a draft or a
  pre-release), and its *Tests* and *Windows build* runs are green.
- `custom_components/pclink/manifest.json`, `pyproject.toml` and
  `pclink/__init__.py` have the same version as that release.
- History check (done 2026-11-27): no secrets in any branch or tag. Every
  pushed commit is authored with the GitHub no-reply address
  `70563667+cslehel@users.noreply.github.com`. Before your first public push,
  turn on **GitHub → Settings → Emails → "Keep my email addresses private"**
  and **"Block command line pushes that expose my email"**, so this stays true.

## 2. Make the repository public

GitHub → repository → **Settings → General → Danger Zone → Change visibility →
Public**, or:

```bash
gh repo edit cslehel/home-assistant-pc-link --visibility public --accept-visibility-change-consequences
```

The code, the full history, the issues and the releases become visible to
everyone. The PCs keep updating as before. Their `updates.github_token` is no
longer needed and can be removed from `config.yaml` afterwards.

## 3. Description, topics, issues

The repository must have a description and topics, and issues must be enabled:

```bash
gh repo edit cslehel/home-assistant-pc-link --enable-issues \
  --description "Home Assistant integration + Windows service: per-user screen time (multiseat: ASTER, Hydra), presence, telemetry, power control" \
  --add-topic home-assistant --add-topic hacs --add-topic integration \
  --add-topic parental-controls --add-topic screen-time --add-topic multiseat --add-topic windows
```

(The repository name must not contain "HACS"; ours does not.)

## 4. Let the validations run

The *hacs* job in the **Tests** workflow is skipped while the repository is
private, so it runs for the first time now: **Actions → Tests → Run workflow**
on `main`. Both jobs below must pass, with no `ignore` (the catalog accepts
none):

- **hacs**: `hacs/action`, category `integration`
- **hassfest**: Home Assistant's manifest, translation and brand-image check.
  The catalog fails a submission on any `[BRAND_IMAGES]` warning.

Keep the links of both green runs; the pull request asks for them.

## 5. Make a new release after the validations passed

The submission checklist asks for a release made **after** the actions passed.
Bump the version (e.g. 0.5.1) in `pclink/__init__.py`, `manifest.json` and
`pyproject.toml`, add a CHANGELOG section (e.g. "Hydra confirmed on a real PC"),
push, then **Actions → Release → Run workflow** (or push the tag `v0.5.1`).
Check that the release is a full release with the Windows zip attached.

## 6. Open the pull request to hacs/default

From your **personal** account (not an organisation), so the maintainers can
edit the branch:

```bash
gh repo fork hacs/default --clone
cd default
git checkout -b add-pc-link master
python - <<'EOF'
import json
path = "integration"
items = json.load(open(path, encoding="utf-8"))
items.append("cslehel/home-assistant-pc-link")
items = sorted(set(items), key=str.casefold)        # alphabetical, not at the end
open(path, "w", encoding="utf-8").write(json.dumps(items, indent=2) + "\n")
EOF
git diff --stat            # exactly one line added
git commit -am "Adds new integration [cslehel/home-assistant-pc-link]"
git push -u origin add-pc-link
gh pr create --repo hacs/default --base master --fill-first
```

Then edit the pull request text so it follows the template
(`.github/PULL_REQUEST_TEMPLATE.md`): tick each item and add the three links:

- the release: `https://github.com/cslehel/home-assistant-pc-link/releases/tag/v0.5.1`
- the HACS action run (from step 4)
- the hassfest run (from step 4)

Before saving, check that the indentation of the `integration` file matches
the rest of the file (`git diff` shows only your one line).

## 7. Waiting for the review

- Do not comment, request reviews or open a second pull request; that can get
  it closed. The bots check the owner, the releases, the domain and both
  actions.
- The review is a real code review by a maintainer. Typical requests are
  blocking I/O in the event loop, admin-only actions, unsafe HTML, and
  binaries not built in CI. Our Windows zip is built from source by
  `release.yml`, and the integration only talks to the PC over the network.
- If it is turned into a draft, fix the problem, make a new release, and mark
  it *Ready for review* again.
- In November 2026 many submissions were merged within days. The documentation
  still warns that it can take months.

## 8. After the merge

- It shows up in **Settings → Marketplace** within about 8 hours.
- On your own Home Assistant, install **PC Link** from the Marketplace over the
  hand-copied `config/custom_components/pclink`, then restart Home Assistant.
  Entities and settings are kept, since the domain is the same.
- Optional: add the Marketplace badge to the README.
