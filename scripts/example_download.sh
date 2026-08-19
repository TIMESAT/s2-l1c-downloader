#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-config/vombsjon.yaml}"

s2l1c search --config "$CONFIG"
s2l1c inventory --config "$CONFIG"
s2l1c download --config "$CONFIG" --year 2024 --dry-run

# After reviewing the catalogue and dry run, start the real transfer with:
# s2l1c download --config "$CONFIG" --year 2024 --yes

