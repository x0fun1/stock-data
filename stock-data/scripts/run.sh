#!/usr/bin/env bash
# stock-data Skill launcher: one regular entry point for the native runtime.
#
# The Skill directory is resolved from this script's own location, so the caller's
# working directory (and relative request/output paths) is preserved. Cargo builds into a
# user-writable cache outside the Skill, keeping the mounted Skill read-only-friendly.
set -euo pipefail

SCRIPT_DIR="$(
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P
)"
SKILL_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd -P)"
RUST_DIR="$SKILL_ROOT/scripts/rust"
MANIFEST="$RUST_DIR/Cargo.toml"

# The toolchain is part of the workspace contract (rust-toolchain.toml pins 1.99.1).
# `cargo --manifest-path` does not select a rustup toolchain by itself, so the launcher
# exports it explicitly; rustup-less system toolchains are version-checked below.
if command -v rustup >/dev/null 2>&1; then
  export RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-1.99.1}"
fi

CACHE_ROOT="${STOCK_DATA_RUST_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/stock-data}"
TARGET_DIR="${STOCK_DATA_TARGET_DIR:-$CACHE_ROOT/target}"
mkdir -p "$TARGET_DIR"

if ! command -v cargo >/dev/null 2>&1; then
  echo "stock-data: cargo not found. Run \"$SCRIPT_DIR/setup.sh\" once on this host." >&2
  exit 127
fi

cargo_version="$(cargo --version | awk '{print $2}')"
if ! awk -v v="$cargo_version" 'BEGIN {
  split(v, parts, ".")
  exit !((parts[1] > 1) || (parts[1] == 1 && parts[2] >= 99))
}'; then
  echo "stock-data: Rust $cargo_version is older than the required 1.99 toolchain." >&2
  echo "Run \"$SCRIPT_DIR/setup.sh\" after installing Rust >= 1.99 (MSRV in Cargo.toml)." >&2
  exit 1
fi

export STOCK_DATA_SKILL_ROOT="${STOCK_DATA_SKILL_ROOT:-$SKILL_ROOT}"

# argv passthrough: no eval, no string concatenation, real exit status and signals.
exec cargo run \
  --release \
  --locked \
  --offline \
  --manifest-path "$MANIFEST" \
  --target-dir "$TARGET_DIR" \
  -- "$@"
