#!/bin/sh
# Install the latest sf-exit single-file binary.
#
#   curl -fsSL https://raw.githubusercontent.com/akhsterminator/sf-exit/main/install.sh | sh
#
# Installs into ~/.local/bin (override with SF_EXIT_INSTALL_DIR). Verifies the SHA-256
# checksum published with the release. Prefer pipx if you have Python: pipx install sf-exit
set -eu

REPO="akhsterminator/sf-exit"
DIR="${SF_EXIT_INSTALL_DIR:-$HOME/.local/bin}"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "Unsupported OS: $(uname -s). Use: pipx install sf-exit" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) echo "Unsupported CPU: $(uname -m). Use: pipx install sf-exit" >&2; exit 1 ;;
esac
if [ "$os" = macos ] && [ "$arch" = x86_64 ]; then
  echo "No Intel Mac binary is published. Use: pipx install sf-exit" >&2; exit 1
fi

asset="sf-exit-$os-$arch"
base="https://github.com/$REPO/releases/latest/download"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading $asset ..."
curl -fsSL "$base/$asset" -o "$tmp/$asset"
curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"
expected="$(grep " $asset\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)"
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$tmp/$asset" | cut -d' ' -f1)"
else
  actual="$(shasum -a 256 "$tmp/$asset" | cut -d' ' -f1)"
fi
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "Checksum verification failed for $asset" >&2
  exit 1
fi

mkdir -p "$DIR"
install -m 755 "$tmp/$asset" "$DIR/sf-exit"
echo "Installed sf-exit $("$DIR/sf-exit" --version | awk '{print $NF}') to $DIR/sf-exit"
case ":$PATH:" in
  *":$DIR:"*) ;;
  *) echo "Add $DIR to your PATH, e.g.: export PATH=\"$DIR:\$PATH\"" ;;
esac
