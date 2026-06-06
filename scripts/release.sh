#!/usr/bin/env bash
# Bump version across all packages (lockstep versioning).
# Usage: ./scripts/release.sh 0.2.0

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 0.2.0"
    exit 1
fi

VERSION="$1"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Bumping all packages to v$VERSION"

# Root pyproject.toml
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" "$REPO_ROOT/pyproject.toml"

# Core
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" "$REPO_ROOT/core/pyproject.toml"
sed -i "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" "$REPO_ROOT/core/src/aitelier/__init__.py"

# Python SDK
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" "$REPO_ROOT/sdks/python/pyproject.toml"
sed -i "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" "$REPO_ROOT/sdks/python/src/aitelier_client/__init__.py"

# TypeScript SDK
cd "$REPO_ROOT/sdks/typescript"
if command -v npm &>/dev/null; then
    npm version "$VERSION" --no-git-tag-version
else
    sed -i "s/\"version\": \".*\"/\"version\": \"$VERSION\"/" package.json
fi

# Root package.json
sed -i "s/\"version\": \".*\"/\"version\": \"$VERSION\"/" "$REPO_ROOT/package.json"

echo ""
echo "Updated to v$VERSION:"
echo "  - pyproject.toml (root, core, python SDK)"
echo "  - package.json (root, typescript SDK)"
echo "  - __init__.py (core, python SDK)"
echo ""
echo "Next steps:"
echo "  1. Update CHANGELOG.md"
echo "  2. git commit -m \"release: v$VERSION\""
echo "  3. git tag v$VERSION"
