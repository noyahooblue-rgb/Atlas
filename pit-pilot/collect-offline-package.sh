#!/usr/bin/env bash
#
# collect-offline-package.sh
#
# Collects every file listed in offline-manifest.tsv from a local
# Maven repository into a self-contained offline package:
#
#   <OUTPUT_DIR>/
#     repo/                 a local-repository directory holding
#                           exactly the manifest files, laid out
#                           like a normal ~/.m2/repository
#     pit-offline-repo.zip  the same repo/ directory zipped
#     sha256sums.txt        sha256 of every file in repo/, plus the zip
#
# Usage:
#   ./collect-offline-package.sh [SOURCE_REPO] [OUTPUT_DIR]
#
#   SOURCE_REPO  local repository to copy from
#                (default: $MAVEN_REPO_LOCAL, else ~/.m2/repository)
#   OUTPUT_DIR   where to write the package (default: ./offline-package)
#
# After collecting, use the package on an offline machine by copying
# repo/ anywhere and running Maven with:
#   mvn -o -Dmaven.repo.local=<path-to-repo> ...
#
# Requires: bash, sha256sum (coreutils), and zip or python3.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${MANIFEST:-$SCRIPT_DIR/offline-manifest.tsv}"
SRC_REPO="${1:-${MAVEN_REPO_LOCAL:-$HOME/.m2/repository}}"
OUT_DIR="${2:-./offline-package}"
REPO_OUT="$OUT_DIR/repo"

if [[ ! -f "$MANIFEST" ]]; then
  echo "manifest not found: $MANIFEST" >&2
  exit 1
fi
if [[ ! -d "$SRC_REPO" ]]; then
  echo "source repository not found: $SRC_REPO" >&2
  exit 1
fi

echo "source repository : $SRC_REPO"
echo "manifest          : $MANIFEST"
echo "output directory  : $OUT_DIR"

rm -rf "$REPO_OUT"
mkdir -p "$REPO_OUT"

count=0
total=0
missing=0
while IFS=$'\t' read -r relpath _group _artifact _version _type size _rest; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac
  src="$SRC_REPO/$relpath"
  if [[ ! -f "$src" ]]; then
    echo "MISSING: $relpath" >&2
    missing=$((missing + 1))
    continue
  fi
  mkdir -p "$REPO_OUT/$(dirname "$relpath")"
  cp -p "$src" "$REPO_OUT/$relpath"
  count=$((count + 1))
  total=$((total + size))
done < "$MANIFEST"

if [[ "$missing" -gt 0 ]]; then
  echo "ERROR: $missing manifest file(s) missing from source repository" >&2
  exit 1
fi

echo "collected files   : $count"
echo "total size (bytes): $total"

# sha256 for every collected file (paths relative to repo/).
(
  cd "$REPO_OUT"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
) > "$OUT_DIR/sha256sums.txt"

# Zip the repository directory.
ZIP_FILE="$OUT_DIR/pit-offline-repo.zip"
rm -f "$ZIP_FILE"
if command -v zip >/dev/null 2>&1; then
  (cd "$OUT_DIR" && zip -qr "pit-offline-repo.zip" repo)
else
  python3 - "$REPO_OUT" "$ZIP_FILE" <<'PYEOF'
import os, sys, zipfile
root, zpath = sys.argv[1], sys.argv[2]
base = os.path.dirname(root)
with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            z.write(full, os.path.relpath(full, base))
PYEOF
fi

zip_size=$(stat -c%s "$ZIP_FILE")
zip_sha=$(sha256sum "$ZIP_FILE" | awk '{print $1}')
echo "pit-offline-repo.zip  $zip_size bytes  sha256:$zip_sha" >> "$OUT_DIR/sha256sums.txt"

echo "zip               : $ZIP_FILE ($zip_size bytes)"
echo "sha256 list       : $OUT_DIR/sha256sums.txt"
echo "done."
