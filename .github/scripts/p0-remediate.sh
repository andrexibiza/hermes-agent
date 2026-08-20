#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import json
import re
from pathlib import Path


def load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path: str, data):
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


root = load("package.json")
overrides = root.setdefault("overrides", {})
overrides.pop("nanoid@^3", None)
overrides["postcss"] = {".": "8.5.23", "nanoid": "3.3.18"}
allow_scripts = root.setdefault("allowScripts", {})
allow_scripts.pop("electron@40.10.2", None)
allow_scripts["electron@41.10.3"] = True
write("package.json", root)

website = load("website/package.json")
website.setdefault("overrides", {})["nanoid"] = "3.3.18"
write("website/package.json", website)

desktop = load("apps/desktop/package.json")
desktop["devDependencies"]["electron"] = "41.10.3"
desktop["build"]["electronVersion"] = "41.10.3"
write("apps/desktop/package.json", desktop)

npmrc = Path(".npmrc")
text = npmrc.read_text(encoding="utf-8")
text = re.sub(
    r"# nanoid 3\.3\.17 includes fixes for GHSA-2v37-7h3g-55p8\.[^\n]*",
    "# nanoid 3.3.18 fixes GHSA-2v37-7h3g-55p8. remove when > 2wks old (rel 2026-08-07)",
    text,
)
npmrc.write_text(text, encoding="utf-8")

Path("apps/desktop/scripts/packaged-app-layout.mjs").write_text(
    "export function resolveLinuxUnpackedDirName(arch) {\n"
    "  return arch === 'x64' ? 'linux-unpacked' : `linux-${arch}-unpacked`\n"
    "}\n",
    encoding="utf-8",
)
Path("apps/desktop/scripts/packaged-app-layout.test.mjs").write_text(
    "import assert from 'node:assert/strict'\n"
    "import test from 'node:test'\n\n"
    "import { resolveLinuxUnpackedDirName } from './packaged-app-layout.mjs'\n\n"
    "test('uses electron-builder default directory on Linux x64', () => {\n"
    "  assert.equal(resolveLinuxUnpackedDirName('x64'), 'linux-unpacked')\n"
    "})\n\n"
    "test('includes architecture in electron-builder Linux ARM64 directory', () => {\n"
    "  assert.equal(resolveLinuxUnpackedDirName('arm64'), 'linux-arm64-unpacked')\n"
    "})\n",
    encoding="utf-8",
)

target = Path("apps/desktop/scripts/test-desktop.mjs")
text = target.read_text(encoding="utf-8")
anchor = "import PACKAGE_JSON from '../package.json' with { type: 'json' }"
helper_import = "import { resolveLinuxUnpackedDirName } from './packaged-app-layout.mjs'"
if helper_import not in text:
    if anchor not in text:
        raise SystemExit("test-desktop import anchor not found")
    text = text.replace(anchor, helper_import + "\n" + anchor, 1)
old = "  // linux unpacked layout matches windows but with different binary name\n  const unpacked = path.join(RELEASE_ROOT, 'linux-unpacked')"
new = "  // electron-builder includes the architecture suffix for non-x64 Linux output.\n  const unpacked = path.join(RELEASE_ROOT, resolveLinuxUnpackedDirName(ARCH))"
if old in text:
    text = text.replace(old, new, 1)
elif new not in text:
    raise SystemExit("test-desktop Linux layout anchor not found")
target.write_text(text, encoding="utf-8")

osv = Path(".github/workflows/osv-scanner.yml")
text = osv.read_text(encoding="utf-8")
text = text.replace(
    "# This is detection-only — OSV-Scanner does NOT open PRs or modify pins.\n"
    "# It reports known CVEs in currently-pinned dependency versions so we can\n"
    "# decide when and how to patch on our own schedule. Our pinning strategy\n"
    "# (full SHA / exact version) is preserved; only the notification signal\n"
    "# is added.\n",
    "# OSV-Scanner is a merge boundary for known vulnerabilities in the pinned\n"
    "# dependency graph. It never modifies pins; remediation remains an explicit\n"
    "# reviewed repository change.\n",
)
text = text.replace(
    "# fail-on-vuln is disabled so the job does not block merges on pre-existing\n"
    "# vulnerabilities in pinned deps that we may need to patch deliberately.\n",
    "# Known vulnerabilities fail the required lane. A green result therefore\n"
    "# means the exact scanned dependency graph completed without OSV findings.\n",
)
if "fail-on-vuln: false" not in text:
    raise SystemExit("OSV fail-on-vuln anchor not found")
text = text.replace("fail-on-vuln: false", "fail-on-vuln: true", 1)
text = text.replace(
    '\"kind\":\"warning\",\"title\":\"OSV vulnerability scan\"',
    '\"kind\":\"error\",\"title\":\"OSV vulnerability scan\"',
)
osv.write_text(text, encoding="utf-8")
PY

npm install --package-lock-only --ignore-scripts --no-audit --no-fund
npm --prefix website install --package-lock-only --ignore-scripts --no-audit --no-fund
uv lock --upgrade-package h2

uv lock --check
npm ci --ignore-scripts --no-audit --no-fund
npm --prefix website ci --ignore-scripts --no-audit --no-fund

npm audit --workspaces=false --audit-level=moderate
npm audit --workspace web --audit-level=moderate
npm audit --workspace ui-tui --audit-level=moderate
npm audit --workspace apps/desktop --audit-level=moderate
npm --prefix website audit --audit-level=moderate

node --test apps/desktop/scripts/packaged-app-layout.test.mjs
node <<'NODE'
const lock = require('./package-lock.json')
const root = require('./package.json')
const desktop = require('./apps/desktop/package.json')
const website = require('./website/package.json')

const postcssOverride = root.overrides.postcss
if (root.overrides['nanoid@^3'] !== undefined) throw new Error('flat nanoid v3 override remains')
if (postcssOverride?.['.'] !== '8.5.23' || postcssOverride?.nanoid !== '3.3.18') {
  throw new Error(`postcss override is ${JSON.stringify(postcssOverride)}`)
}
if (root.overrides['nanoid@^6'] !== '6.0.0') throw new Error('nanoid v6 isolation changed')
if (website.overrides.nanoid !== '3.3.18') throw new Error('website nanoid override is not 3.3.18')
if (desktop.devDependencies.electron !== '41.10.3') throw new Error('Electron manifest is not 41.10.3')
if (desktop.build.electronVersion !== '41.10.3') throw new Error('Electron build version is not 41.10.3')
if (root.allowScripts['electron@40.10.2'] !== undefined) throw new Error('old Electron install-script grant remains')
if (root.allowScripts['electron@41.10.3'] !== true) throw new Error('new Electron install-script grant missing')

const electron = lock.packages['node_modules/electron'] || lock.packages['apps/desktop/node_modules/electron']
if (!electron || electron.version !== '41.10.3') throw new Error(`Electron lock is ${electron?.version}`)
for (const [path, pkg] of Object.entries(lock.packages)) {
  if (path.endsWith('/nanoid') && pkg.version?.startsWith('3.') && pkg.version !== '3.3.18') {
    throw new Error(`${path} still resolves nanoid ${pkg.version}`)
  }
  if (path.endsWith('/extract-zip') && pkg.version === '2.0.1') {
    throw new Error(`${path} still resolves extract-zip 2.0.1`)
  }
}
NODE
python3 - <<'PY'
import re
lock = open('uv.lock', encoding='utf-8').read()
match = re.search(r'\[\[package\]\]\nname = "h2"\nversion = "([^"]+)"', lock)
if not match or match.group(1) != '4.4.1':
    raise SystemExit(f"h2 lock is {match.group(1) if match else 'missing'}")
PY

git diff --check
rm -f .github/workflows/p0-dependency-remediation.yml .github/scripts/p0-remediate.sh
git config user.name "Axl Ibiza"
git config user.email "andrexibiza@gmail.com"
git add .
git diff --cached --check
git commit -m "fix(deps): close active advisory set and enforce OSV" \
  -m "Co-authored-by: schmitzi8 <281458983+schmitzi8@users.noreply.github.com>" \
  -m "Co-authored-by: orcaspainting-dev <264355715+orcaspainting-dev@users.noreply.github.com>"
git push origin HEAD:security/p0-dependency-remediation-20260820
