#!/usr/bin/env bash

set -uo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${script_dir}/.." && pwd)
archive_root="${S2VOMB_DOWNLOAD_DIRECTORY:-${repo_root}/data/raw/S2_L1C}"
keep_zip=false
dry_run=false

usage() {
  cat <<'EOF'
Usage: scripts/extract_verified.sh [OPTIONS]

Safely extract Sentinel-2 *.SAFE.zip archives. Each ZIP is tested and its
extracted SAFE structure is verified before the source ZIP is deleted.

Options:
  --directory DIR  Archive root to scan (default: data/raw/S2_L1C)
  --keep-zip       Keep the verified source ZIP after extraction
  --dry-run        List archives without extracting or deleting anything
  -h, --help       Show this help
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
    --keep-zip)
      keep_zip=true
      shift
      ;;
    --dry-run)
      dry_run=true
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

for command_name in find unzip mktemp mv rm rmdir; do
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
log_file="${log_directory}/extract-${timestamp}.log"

log() {
  printf '%s\n' "$*" | tee -a "$log_file"
}

verify_safe() {
  local safe_path=$1
  [[ -s "${safe_path}/manifest.safe" ]] || return 1
  [[ -s "${safe_path}/MTD_MSIL1C.xml" ]] || return 1
  [[ -d "${safe_path}/GRANULE" ]] || return 1
  [[ -n "$(find "${safe_path}/GRANULE" -type f -name '*.jp2' -print -quit)" ]]
}

archive_entries_are_safe() {
  local archive=$1
  local expected_root=$2
  unzip -Z1 "$archive" | awk -v root="${expected_root}/" '
    index($0, root) != 1 { invalid = 1 }
    END { exit invalid }
  '
}

processed=0
extracted=0
removed=0
skipped=0
failed=0

while IFS= read -r -d '' archive; do
  processed=$((processed + 1))
  safe_path=${archive%.zip}
  safe_name=$(basename -- "$safe_path")
  parent=$(dirname -- "$archive")

  if $dry_run; then
    log "WOULD_EXTRACT: ${archive}"
    continue
  fi

  if ! unzip -tq "$archive" >/dev/null; then
    log "FAILED_ZIP_TEST: ${archive}"
    failed=$((failed + 1))
    continue
  fi
  if ! archive_entries_are_safe "$archive" "$safe_name"; then
    log "FAILED_UNSAFE_LAYOUT: ${archive}"
    failed=$((failed + 1))
    continue
  fi

  if [[ -e "$safe_path" ]]; then
    if ! verify_safe "$safe_path"; then
      log "FAILED_EXISTING_SAFE_INCOMPLETE: ${safe_path}"
      failed=$((failed + 1))
      continue
    fi
    skipped=$((skipped + 1))
    if $keep_zip; then
      log "ALREADY_EXTRACTED_KEPT_ZIP: ${safe_path}"
    else
      rm -- "$archive"
      removed=$((removed + 1))
      log "ALREADY_EXTRACTED_REMOVED_ZIP: ${safe_path}"
    fi
    continue
  fi

  temp_dir=$(mktemp -d "${parent}/.${safe_name}.extracting.XXXXXX")
  if ! unzip -q "$archive" -d "$temp_dir"; then
    rm -rf -- "$temp_dir"
    log "FAILED_EXTRACTION: ${archive}"
    failed=$((failed + 1))
    continue
  fi
  extracted_safe="${temp_dir}/${safe_name}"
  if ! verify_safe "$extracted_safe"; then
    rm -rf -- "$temp_dir"
    log "FAILED_SAFE_VERIFICATION: ${archive}"
    failed=$((failed + 1))
    continue
  fi

  mv -- "$extracted_safe" "$safe_path"
  rmdir -- "$temp_dir"
  extracted=$((extracted + 1))
  if $keep_zip; then
    log "EXTRACTED_KEPT_ZIP: ${safe_path}"
  else
    rm -- "$archive"
    removed=$((removed + 1))
    log "EXTRACTED_AND_REMOVED_ZIP: ${safe_path}"
  fi
done < <(find "$archive_root" -type f -name '*.SAFE.zip' -print0)

log "SUMMARY: scanned=${processed} extracted=${extracted} existing=${skipped} zip_removed=${removed} failed=${failed}"
log "LOG: ${log_file}"

if ((failed > 0)); then
  exit 1
fi
