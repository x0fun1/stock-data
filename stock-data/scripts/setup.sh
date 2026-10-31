#!/usr/bin/env bash
# stock-data Skill setup: verify the pinned toolchain, fetch locked dependencies, and
# pre-build the native runtime. Network is expected here only; regular runs are offline.
set -euo pipefail

SCRIPT_DIR="$(
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P
)"
SKILL_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd -P)"
RUST_DIR="$SKILL_ROOT/scripts/rust"
MANIFEST="$RUST_DIR/Cargo.toml"

if command -v rustup >/dev/null 2>&1; then
  export RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-1.99.1}"
  # The workspace pins 1.99.1; rustup does not install a toolchain from
  # `cargo --manifest-path`, so setup makes the pinned toolchain explicit.
  if ! rustup toolchain list | grep -q "^${RUSTUP_TOOLCHAIN}"; then
    rustup toolchain install "$RUSTUP_TOOLCHAIN" --profile minimal --component rustfmt,clippy
  fi
fi

CACHE_ROOT="${STOCK_DATA_RUST_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/stock-data}"
TARGET_DIR="${STOCK_DATA_TARGET_DIR:-$CACHE_ROOT/target}"
mkdir -p "$TARGET_DIR" "$CACHE_ROOT"

# On Windows the MSVC toolchain supplies link.exe, so a POSIX `cc` driver is not a
# setup prerequisite (the host still needs a Rust toolchain and, when linking with
# MSVC, the Visual Studio build tools that rustc discovers itself). POSIX hosts need a
# C compiler/linker for native TLS dependencies.
required_tools="cargo rustc"
case "$(uname -s 2>/dev/null || true)" in
  MINGW*|MSYS*|CYGWIN*|Windows_NT*) ;;
  *) required_tools="$required_tools cc" ;;
esac

for tool in $required_tools; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "stock-data setup: missing build tool: $tool" >&2
    case "$tool" in
      cargo|rustc) echo "Install Rust >= 1.99 (e.g. rustup) and re-run setup.sh." >&2 ;;
      cc) echo "Install a C toolchain (build-essential on Debian) and re-run setup.sh." >&2 ;;
    esac
    exit 1
  fi
done

echo "stock-data setup: $(rustc --version)"
echo "stock-data setup: $(cargo --version)"

cargo_version="$(cargo --version | awk '{print $2}')"
if ! awk -v v="$cargo_version" 'BEGIN {
  split(v, parts, ".")
  exit !((parts[1] > 1) || (parts[1] == 1 && parts[2] >= 99))
}'; then
  echo "stock-data setup: Rust $cargo_version is older than the required 1.99 toolchain." >&2
  exit 1
fi

# Locked dependency fetch may use the network; normal runs never do.
cargo fetch --locked --manifest-path "$MANIFEST"
cargo build --release --locked --offline \
  --manifest-path "$MANIFEST" --target-dir "$TARGET_DIR"

echo "stock-data setup: prebuilt native runtime"
echo "  cache:  $TARGET_DIR"
echo "  run:    $SCRIPT_DIR/run.sh --help"
