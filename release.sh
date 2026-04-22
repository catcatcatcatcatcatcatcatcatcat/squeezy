#!/usr/bin/env bash
# release.sh — bump version, tag, push, upload to PyPI.
# The GitHub Actions workflow handles the Homebrew tap and GitHub Release automatically.
#
# Usage:
#   ./release.sh patch    # 0.4.0 → 0.4.1
#   ./release.sh minor    # 0.4.0 → 0.5.0
#   ./release.sh major    # 0.4.0 → 1.0.0

set -euo pipefail

BUMP="${1:-}"
if [[ "$BUMP" != patch && "$BUMP" != minor && "$BUMP" != major ]]; then
    echo "Usage: $0 [patch|minor|major]" >&2
    exit 1
fi

# ── 1. Read current version from pyproject.toml ────────────────────────────
CURRENT=$(python3 - <<'EOF'
import tomllib
with open("pyproject.toml", "rb") as f:
    print(tomllib.load(f)["project"]["version"])
EOF
)

# ── 2. Compute new version ──────────────────────────────────────────────────
NEW=$(python3 - <<EOF
major, minor, patch = map(int, "$CURRENT".split("."))
if "$BUMP" == "major":
    print(f"{major+1}.0.0")
elif "$BUMP" == "minor":
    print(f"{major}.{minor+1}.0")
else:
    print(f"{major}.{minor}.{patch+1}")
EOF
)

echo "Releasing: $CURRENT → $NEW ($BUMP bump)"
read -r -p "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── 3. Bump version in pyproject.toml ──────────────────────────────────────
python3 - <<EOF
import re
with open("pyproject.toml") as f:
    content = f.read()
content = re.sub(r'^version = "[^"]+"', 'version = "$NEW"', content, count=1, flags=re.MULTILINE)
with open("pyproject.toml", "w") as f:
    f.write(content)
print(f"pyproject.toml updated to {repr('$NEW')}")
EOF

# ── 4. Commit, tag, push ────────────────────────────────────────────────────
git add pyproject.toml
git commit -m "chore: release v$NEW"
git tag "v$NEW"
git push
git push origin "v$NEW"
echo "↑ tag v$NEW pushed — GitHub Actions will update Homebrew tap automatically"

# ── 5. Build and upload to PyPI ────────────────────────────────────────────
echo ""
echo "Building..."
rm -rf dist/ build/
pipx run build

echo ""
echo "Uploading to PyPI..."
pipx run twine upload dist/*

echo ""
echo "Done! v$NEW is live on PyPI. Homebrew tap update running in CI."
