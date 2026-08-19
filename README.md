# Sentinel-2 L1C archive for Vombsjön

`s2vomb` discovers, catalogues, inventories, and bulk downloads complete Sentinel-2
Level-1C source products for Vombsjön, Skåne, Sweden. Discovery uses the official
[Copernicus Data Space Ecosystem STAC API](https://documentation.dataspace.copernicus.eu/APIs/STAC.html),
and complete products are retrieved through the official
[CDSE OData download service](https://documentation.dataspace.copernicus.eu/APIs/OData.html#product-download).
It does not scrape the CDSE web interface.

The repository is designed for a Sentinel-2 × TIMESAT × chlorophyll-a study. Its archive is
atmospheric-correction-neutral: downloads are preserved as original, complete L1C product ZIPs
and can later feed ACOLITE-DSF, ESA SNAP/C2RCC/C2X, or direct top-of-atmosphere analyses.

## Scientific design

Vombsjön covers only a small part of a Sentinel-2 tile. Scene/tile-level cloud percentage can
therefore be a poor proxy for conditions over the lake. The default configuration applies **no
cloud threshold**. With the supplied configuration it catalogues every L1C product in the
specified MGRS tile and records `eo:cloud_cover` for later review. An optional, permissive
threshold can be configured, but ROI-specific cloud screening belongs in a downstream workflow.

Two spatial concepts are versioned in [`config/vombsjon.geojson`](config/vombsjon.geojson):

- `vombsjon-search` is a compact WGS84 polygon used as the discovery fallback when no tile is set.
- `vombsjon-processing-roi-5km` is an approximate lake-plus-5 km context ROI stored for later
  processing. It never crops the source download.

These polygons are practical starting geometries, not an authoritative hydrographic boundary.
Replace them with a validated project boundary before a final publication analysis if required;
the exact geometry and its SHA-256 are captured in every run manifest.

## Architecture

The workflow is deliberately staged:

1. `search` sends the configured tile/date/filter query to the public CDSE STAC 1.1 API. If
   `tile_id` is null, it falls back to the configured polygon.
2. STAC items are normalized to a chronological CSV catalogue; raw STAC JSON and optional
   Parquet are also retained.
3. `inventory` reports product counts, temporal/platform/tile distributions, scene cloud
   metadata, download state, and estimated storage.
4. `download` reads that local catalogue, shows the selected inventory, and retrieves complete
   archives through authenticated OData URLs. It never performs discovery implicitly.
5. Every run writes an effective configuration, exact query geometry, manifest, log, and relevant
   catalogue/report snapshot under `data/logs/runs/`. Download runs also record every selected
   product and outcome in `download-results.json`.

The normalized catalogue includes product UUID and name, STAC ID, acquisition time, platform,
MGRS tile, processing level, scene cloud cover, full-product URL, byte size, online status when
reported, multihash checksum and algorithm, item link/footprint, search-geometry identity, local
download state, local path, transferred bytes, verification state, attempts, and last error.

## Installation

Python 3.11 or newer is required.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[parquet]'
```

The core install writes CSV and raw STAC JSON. The `parquet` extra adds PyArrow. For development:

```bash
python -m pip install -e '.[parquet,dev]'
pytest
ruff check .
```

## CDSE account and authentication

Public STAC search and all dry runs require no credentials. Full-product downloads require a
free [CDSE account](https://dataspace.copernicus.eu/) and an OAuth access token. The application
uses the official `cdse-public` password grant and refreshes the returned access token in memory.
It never writes credentials, access tokens, or refresh tokens to disk.

Set these environment variables in the shell that launches `s2vomb`:

- `CDSE_USERNAME`
- `CDSE_PASSWORD`
- `CDSE_TOTP` only when the account requires a time-based 2FA code

A short-lived `CDSE_ACCESS_TOKEN` may be supplied instead. If CDSE rejects it, username/password
must be available for reauthentication. Use a shell prompt, operating-system keychain, or another
local secret manager rather than putting values in shell history. `.env.example` lists variable
names only; `.env*` files are ignored, and the application intentionally does not auto-load them.

Linux/HPC users can override the archive location without editing the tracked YAML:

```bash
export S2VOMB_DOWNLOAD_DIRECTORY=/projects/eko/fs7/pers/ZC/TWIN_water/S2L1C
```

This path overrides `download.directory` for both `s2vomb` and
`scripts/extract_verified.sh`. The resolved absolute path is recorded in every effective
configuration and manifest. It may be placed in the user's ignored `.env`, but that file must
still be sourced before running commands.

## Vombsjön configuration

[`config/vombsjon.yaml`](config/vombsjon.yaml) starts at 2017-01-01 and uses `end_date: null`, which
means the current UTC date at run time. The resolved date is written to the effective configuration
and manifest, so an open-ended query remains reproducible.

Relevant optional filters are:

```yaml
sentinel:
  collection: sentinel-2-l1c
  processing_level: L1C
  start_date: "2017-01-01"
  end_date: null
  platform: null                 # e.g. S2A or sentinel-2b
  tile_id: T33UVB               # set null to use the search polygon instead
  max_scene_cloud_cover: null    # leave null for the research archive
```

Paths are resolved from `project.root`, itself resolved relative to the YAML file. API endpoints,
timeouts, page size, layout, workers, retries, backoff, chunk size, and checksum verification are
also explicit. Credentials are never valid configuration keys.

## Search the catalogue

```bash
s2vomb search --config config/vombsjon.yaml
```

This command downloads no imagery. It writes:

- `data/catalogue/catalogue.csv` — authoritative mutable local state;
- `data/catalogue/catalogue.parquet` — when the Parquet extra is installed;
- `data/catalogue/catalogue.stac.json` — original combined STAC features;
- `data/catalogue/inventory.json` — machine-readable storage summary.

A normal terminal summary starts with the study area, L1C level, effective date range, product
count, estimated size, years, and tile IDs. Refreshing the search preserves prior local download
state for matching product UUIDs. Duplicate STAC pagination records are removed deterministically.
`--max-items` exists for diagnostics only and is recorded in provenance; omit it for a publication
catalogue.

## Review storage requirements

```bash
s2vomb inventory --config config/vombsjon.yaml
s2vomb inventory --config config/vombsjon.yaml --year 2024
```

The human summary is accompanied by JSON containing products per year and month, platform and tile
counts, cloud-cover bins/statistics, status counts, total known bytes, and the number of products
whose size was not reported. Review this before committing storage to a multi-year transfer.

## Download

Start with an authentication-free dry run:

```bash
s2vomb download --config config/vombsjon.yaml --year 2024 --dry-run
```

An inclusive short date window can be selected without rebuilding the catalogue:

```bash
s2vomb download --config config/vombsjon.yaml \
  --start-date 2026-08-01 --end-date 2026-08-15 --dry-run
```

For a small cross-year processor test, select the single scene in each year whose scene-level
cloud metadata is nearest a target percentage:

```bash
s2vomb download --config config/vombsjon.yaml --one-per-year-near-cloud 40 --dry-run
```

This is deterministic and records the target in the run manifest. It uses tile/scene cloud
metadata only; it is not ROI-specific cloud screening.

It prints the exact targets without opening a network download. Then launch the real transfer:

```bash
s2vomb download --config config/vombsjon.yaml --year 2024
```

The command prints the selected inventory and asks for confirmation. For a reviewed,
non-interactive job:

```bash
s2vomb download --config config/vombsjon.yaml --year 2024 --yes
```

Omit `--year` only after reviewing the full inventory. The default is two workers with five
retries and exponential backoff; keep concurrency conservative to avoid overloading CDSE.

## Resume and verification behavior

Transfers are written to `*.part`. A rerun sends an HTTP Range request from the existing byte
offset. If the server ignores Range, the partial file is safely restarted. HTTP 401 invalidates the
in-memory token; 408/425/429 and common 5xx responses are retried with backoff and `Retry-After`
support.

Before a product is marked complete, the downloader checks the expected STAC byte size, verifies
that it is a readable ZIP, and verifies supported STAC `file:checksum` multihashes (MD5, SHA-1,
SHA-256, or SHA-512). If size is unavailable, it performs a ZIP CRC scan. The final rename is
atomic. Valid completed products are skipped. An invalid existing final archive is moved to a
recoverable `*.invalid-<timestamp>` name before a new transfer; it is never silently deleted.
State is atomically checkpointed to CSV after each product, and failures are written beside the
download run manifest.

Some older reprocessed products have a stale checksum in STAC. On a checksum mismatch, the
downloader makes one read-only request to the official CDSE OData catalogue, refreshes the current
archive size/checksum, writes that metadata back to the local catalogue, and verifies the same
download again before considering a retry. A genuine mismatch against current OData metadata
still fails normally.

If a matching extracted `.SAFE` directory already exists beside the expected ZIP, the downloader
checks for `manifest.safe`, `MTD_MSIL1C.xml`, `GRANULE`, and JP2 imagery, records the product as
`existing-safe`, and skips the download. The archive checksum cannot be reverified after the ZIP
has been deleted, so `checksum_verified` is deliberately false for this state. Incomplete SAFE
directories are left untouched and do not suppress a download.

On Linux or macOS, verified batch extraction is available as a separate, explicit step:

```bash
scripts/extract_verified.sh --dry-run
scripts/extract_verified.sh
```

The script recursively finds `*.SAFE.zip`, tests ZIP integrity and entry paths, extracts into a
temporary sibling directory, verifies core SAFE metadata and JP2 imagery, atomically installs the
SAFE directory, and only then deletes its ZIP. It writes `data/logs/extract-<timestamp>.log` and
returns a nonzero status if any product fails. Use `--keep-zip` to preserve verified source
archives, or `--directory PATH` to scan another archive root. Deleting the ZIP removes the exact
downloaded byte stream and prevents later archive-checksum revalidation; retaining it remains the
preferred publication-archive policy when storage permits.

## Data organization

```text
data/
├── raw/S2_L1C/<tile>/<original-product-name>.SAFE.zip
├── catalogue/
│   ├── catalogue.csv
│   ├── catalogue.parquet
│   ├── catalogue.stac.json
│   └── inventory.json
└── logs/runs/<run-id>/
    ├── manifest.json
    ├── effective-config.yaml
    ├── query-geometry.geojson
    ├── catalogue.csv
    └── run.log
```

`data/raw/**`, generated catalogues, and logs are ignored by Git. Product names and the complete
archive content are preserved. Archives are not silently unpacked, cropped, converted, or deleted.
The example uses `download.layout: tile`, so products are not divided into year directories. When
this layout is selected, the downloader also recognizes complete products in the former
`<tile>/<YYYY>/` layout and skips them without moving or deleting them.

## Reproducibility and provenance

Every catalogue, inventory, and download invocation creates a run directory immediately. Its
manifest records UTC timestamps, status, package/Python version, source-config SHA-256, exact
effective date/filter values, query-geometry SHA-256, output paths, record counts, selected year,
download/skip/failure counts, and failure report. The exact credential-free effective YAML and
query GeoJSON sit beside it. Search and download runs also snapshot the corresponding CSV.

For a publication release, archive the selected run directory, catalogue snapshot, repository
commit, and unmodified product ZIPs together. Do not rely only on `end_date: null`; cite the
manifest's resolved end date.

## Product packaging and downstream use

The CDSE STAC `Product` asset is an `application/zip` file named
`<Sentinel-product>.SAFE.zip`. It contains the complete SAFE product tree, including product,
datastrip, granule, radiometric/geometric, and manifest metadata required by atmospheric
correction. `s2vomb` stores this ZIP byte-for-byte and does not replace it with band-only files or a
custom format.

- **ESA SNAP / C2RCC / C2X:** SNAP versions commonly open native Sentinel product ZIPs directly.
  If a particular processor/version requires a directory, extract the ZIP to processing scratch
  space and point SNAP at the resulting `.SAFE` product; retain the source ZIP unchanged.
- **ACOLITE-DSF:** ACOLITE releases commonly accept a complete Sentinel-2 `.SAFE` input. If the
  installed release does not accept the ZIP itself, extract a working copy of the full `.SAFE`
  tree. Do not select only JP2 bands or discard XML metadata.
- **Direct L1C TOA calculations:** read bands and metadata from an extracted working copy or a ZIP
  reader while treating the archive as immutable source data.

Processor input behavior can change by release, so validate one product with the exact ACOLITE or
SNAP version used in the study before scheduling a batch. Extraction is a downstream staging step,
not a mutation of this source archive.

## Troubleshooting

- **`Configuration file does not exist` / geometry errors:** run from the repository or use an
  absolute config path. Paths are governed by `project.root`; GeoJSON must use WGS84
  longitude/latitude and closed Polygon/MultiPolygon rings.
- **No products found:** inspect the effective date range and search geometry. Remove tile,
  platform, and cloud filters. Do not infer that an empty result means the lake was cloud-free.
- **HTTP 401:** verify the CDSE account locally, refresh any 2FA code, and ensure the credential
  variables are exported in the same shell. Manual access tokens are short lived.
- **HTTP 429 or 5xx:** rerun later. Partial files resume. Reduce `download.workers`; do not increase
  it aggressively.
- **No Parquet file:** CSV and raw STAC JSON are still complete. Install the `parquet` extra and
  rerun `search`.
- **Checksum mismatch:** keep the `.part` or quarantined invalid archive and rerun. Persistent
  mismatches may indicate changed catalogue metadata or a service issue; retain the run log when
  contacting CDSE support.
- **Catalogue says complete but the file moved:** rerun `download`; missing local archives are
  reset and retrieved again.

Normal tests use mocked HTTP responses and require no CDSE credentials or Sentinel downloads. A
user must still perform the first authenticated product download locally to validate their account,
network path, storage permissions, and exact downstream processor versions.

## License

Code is released under the [MIT License](LICENSE). Copernicus Sentinel data remain subject to the
applicable Copernicus data terms; this software does not relicense downloaded products.
