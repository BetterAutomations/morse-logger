#!/usr/bin/env bash
# Refresh the offline bundle: download every package the installer needs into
# bundle/debs/<codename>/, copy the app source in, and rewrite the checksums.
#
# Run this on a machine WITH internet. The debs are fetched from the Ubuntu
# archive for the suite you name, so this one machine can prepare bundles for
# several releases (the deps differ per release; never mix them).
#
#   ./refresh-bundle.sh                  # this machine's release (e.g. resolute)
#   ./refresh-bundle.sh resolute stonking  # 26.04 and 26.10
#   ./refresh-bundle.sh --force resolute # re-download that suite from scratch
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
bundle="$here/bundle"
force=0
suites=()

for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) suites+=("$arg") ;;
  esac
done
if [[ ${#suites[@]} -eq 0 ]]; then
  . /etc/os-release
  suites=("${VERSION_CODENAME:-}")
fi
[[ -n "${suites[0]:-}" ]] || { echo "cannot tell this release; pass a suite name" >&2; exit 2; }

mkdir -p "$bundle"
for suite in "${suites[@]}"; do
  echo "== $suite =="
  [[ $force -eq 1 ]] && rm -rf "$bundle/debs/$suite"
  "$here/tools/fetch-offline-packages.sh" "$suite"
done

echo "== bundling application =="
rm -rf "$bundle/app"
mkdir -p "$bundle/app"
cp -r "$here/morse_logger" "$bundle/app/"
find "$bundle/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "$here/README.md" "$bundle/app/" 2>/dev/null || true
cp "$here/install.sh" "$bundle/install.sh"
chmod +x "$bundle/install.sh"

echo "== checksums =="
cd "$bundle"
{
  for suite in "${suites[@]}"; do sha256sum debs/"$suite"/*.deb; done
  sha256sum install.sh
  find app -type f -print0 | sort -z | xargs -0 sha256sum
} > checksums.sha256
sha256sum -c --quiet checksums.sha256 && echo "   verified $(wc -l < checksums.sha256) files"

echo "== summary =="
for suite in "${suites[@]}"; do
  printf '   %-10s %3d .deb, %s\n' debs/"$suite" \
      "$(ls -1 debs/"$suite"/*.deb | wc -l)" "$(du -sh debs/"$suite" | cut -f1)"
done
printf '   bundle    : %s total\n' "$(du -sh "$bundle" | cut -f1)"
printf '   location  : %s\n' "$bundle"
