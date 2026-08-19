#!/usr/bin/env bash

set -uo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${script_dir}/.." && pwd)
archive_root="${S2L1C_DOWNLOAD_DIRECTORY:-${S2VOMB_DOWNLOAD_DIRECTORY:-${repo_root}/data/raw/S2_L1C}}"
apply=false

usage() {
  cat <<'EOF'
Usage: scripts/recover_quarantined.sh [OPTIONS]

Review or recover ZIPs renamed to *.SAFE.zip.invalid-<timestamp>. The default
is a non-mutating preview. Use --apply only after reviewing its output.

Options:
  --directory DIR  Archive root to scan (default: configured download root)
  --apply          Perform safe restores and remove byte-identical duplicates
  -h, --help       Show this help

Rules:
  * Missing final ZIP + quarantined ZIP passes full CRC: restore its name.
  * Final ZIP exists + files are byte-identical: remove quarantined duplicate.
  * Different, unreadable, or ambiguous files: retain both and report conflict.
EOF
}

while (($#)); do
  case "$1" in
    --directory)
      if (($# < 2)); then
        printf 'ERROR: --directory requires a value\n' >&2
        exit 2
      fi
      archive_root=$2
      shift 2
      ;;
    --apply)
      apply=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'ERROR: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

for command_name in cmp find mv rm unzip; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'ERROR: required command is unavailable: %s\n' "$command_name" >&2
    exit 2
  fi
done

if [[ ! -d "$archive_root" ]]; then
  printf 'ERROR: archive directory does not exist: %s\n' "$archive_root" >&2
  exit 2
fi

archive_root=$(cd -- "$archive_root" && pwd)
log_directory="${repo_root}/data/logs"
mkdir -p -- "$log_directory"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
log_file="${log_directory}/recover-${timestamp}.log"

log() {
  printf '%s\n' "$*" | tee -a "$log_file"
}

scanned=0
recoverable=0
restored=0
identical=0
removed=0
conflicts=0
corrupt=0

while IFS= read -r -d '' quarantined; do
  scanned=$((scanned + 1))
  final=${quarantined%%.invalid-*}
  if [[ "$final" == "$quarantined" || "$final" != *.SAFE.zip ]]; then
    log "CONFLICT_UNRECOGNIZED_NAME: ${quarantined}"
    conflicts=$((conflicts + 1))
    continue
  fi

  if [[ -e "$final" ]]; then
    if [[ ! -f "$final" ]]; then
      log "CONFLICT_FINAL_NOT_FILE: ${final}"
      conflicts=$((conflicts + 1))
      continue
    fi
    if cmp -s -- "$quarantined" "$final"; then
      identical=$((identical + 1))
      if $apply; then
        rm -- "$quarantined"
        removed=$((removed + 1))
        log "REMOVED_IDENTICAL_DUPLICATE: ${quarantined}"
      else
        log "WOULD_REMOVE_IDENTICAL_DUPLICATE: ${quarantined}"
      fi
    else
      log "CONFLICT_DIFFERENT_FROM_FINAL: ${quarantined}"
      conflicts=$((conflicts + 1))
    fi
    continue
  fi

  if ! unzip -tq "$quarantined" >/dev/null; then
    log "CORRUPT_QUARANTINED_ZIP: ${quarantined}"
    corrupt=$((corrupt + 1))
    continue
  fi
  recoverable=$((recoverable + 1))
  if $apply; then
    mv -- "$quarantined" "$final"
    restored=$((restored + 1))
    log "RESTORED_VERIFIED_ZIP: ${final}"
  else
    log "WOULD_RESTORE_VERIFIED_ZIP: ${quarantined}"
  fi
done < <(find "$archive_root" -type f -name '*.SAFE.zip.invalid-*' -print0)

log "SUMMARY: scanned=${scanned} recoverable=${recoverable} restored=${restored} identical=${identical} removed=${removed} conflicts=${conflicts} corrupt=${corrupt}"
log "LOG: ${log_file}"

if ((conflicts > 0 || corrupt > 0)); then
  exit 1
fi
