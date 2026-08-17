#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/vombsjon.yaml}"

s2vomb search --config "$CONFIG"
s2vomb inventory --config "$CONFIG"
s2vomb download --config "$CONFIG" --year 2024 --dry-run

# After reviewing the catalogue and dry run, start the real transfer with:
# s2vomb download --config "$CONFIG" --year 2024 --yes

