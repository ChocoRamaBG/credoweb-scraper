# Detailed CSV publication contract, version 1.0

Every new published `data` branch commit contains the existing normalized CSV
bundle plus these fixed paths:

| Path | Contents |
| --- | --- |
| `full/profiles.csv.gz` | Exact bytes of the collector's `output/profiles.csv` |
| `full/workplaces.csv.gz` | Exact bytes of the collector's `output/workplaces.csv` |
| `full/manifest.json` | Schema, checksums, counts and link to the normalized snapshot |

The decompressed files use UTF-8 BOM, semicolon delimiters and the Bulgarian
column headers recorded in each manifest entry's `columns` list. Import IDs and
phone numbers as text. The detailed export retains its Excel-protective apostrophe
prefixes. Raw workplace evidence is in `JSON запис`, with its original field path
in `Източник поле`. Available coordinates stay attached to that same workplace;
absence of coordinates means unknown. No coordinates are invented or copied from
another location.

Detailed workplaces intentionally preserve occurrences, so their row count can
exceed the normalized workplace count. Join `ID профил` to profile `ID`. Profile
IDs identify CredoWeb records, never a physician's BLS UIN. Current and historical
employment flags remain distinct. A downstream identity or manual correction
policy is not changed by publication.

## Manifest fields

`full/manifest.json` has these top-level fields:

| Field | Contract |
| --- | --- |
| `schema_version` | String `1.0` |
| `publication_status` | String `complete` |
| `encoding` | String `UTF-8 BOM` |
| `delimiter` | String `;` |
| `snapshot_at` | Exactly the normalized root manifest's `snapshot_at` |
| `normalized_manifest_sha256` | SHA-256 of the exact bytes of the root `manifest.json` |
| `counts` | Object containing integer row counts for `profiles` and `workplaces` |
| `files` | Object with required entries `profiles` and `workplaces` |

Each `files` entry contains:

| Field | Contract |
| --- | --- |
| `filename` | `profiles.csv.gz` or `workplaces.csv.gz`, relative to `full/` |
| `columns` | Ordered array of exact Bulgarian CSV header names |
| `rows` | Number of CSV data rows, excluding the header |
| `bytes` | Compressed file size in bytes |
| `sha256` | SHA-256 of the compressed file |
| `uncompressed_bytes` | Exact CSV size after decompression |
| `uncompressed_sha256` | SHA-256 of the decompressed CSV bytes |

SHA-256 values are lowercase hexadecimal. Gzip uses `mtime=0` and an empty stored
filename, making unchanged CSV bytes produce identical gzip bytes under the same
compression implementation. Compressed files at or above 100 MiB are rejected
before Git publication. The checkpoint release and normalized schema remain
unchanged.

## Import validation

Read every file from the same Git commit. Verify the detailed manifest version,
status and exact normalized-manifest hash, then compressed sizes/hashes. Bound
decompression using `uncompressed_bytes` and verify the resulting size/hash before
parsing. Require the recorded CSV headers and row counts; require unique profile
IDs equal to the normalized profile ID set, matching individual source URLs and
valid workplace parent IDs. A missing/corrupt file must leave the previous import
in place. Replace both detailed inputs as one snapshot only after all checks pass.

`publication_status=complete` describes the integrity of the publication. It does
not assert that all profile details have been collected. `partial_snapshot`,
`collection_status` and record statuses remain in the normalized manifest/tables.

## Producer and recovery

After both local export functions succeed over the same paused collection state,
the collector writes `output/export_snapshot.json`. This local seal binds the
two detailed file hashes to the exact normalized manifest. Every export attempt
invalidates the previous seal first, including attempts where only a raw field
changes. A failed or locked export therefore cannot reuse an old seal.

Once collection has stopped, the existing weekly `validate` and `publish`
commands prepare and verify the compressed files automatically. To prepare them
without GitHub or Git operations:

```sh
python scripts/github_sync.py prepare --output output
```

Older unsealed exports must be regenerated together from their checkpoint:

```sh
python credoweb_scraper.py --output output --export-only --quick-export
python scripts/github_sync.py prepare --output output
```

Use this only after the collector using that output directory has stopped.
`--quick-export` still writes both detailed CSVs; it skips the much larger
field-by-field `details.csv`. Normalized-only export commands do not create a
paired seal. The publisher rejects missing/stale seals, mismatched IDs or URLs,
changed files during staging, bad checksums, or incomplete normalized exports.
It validates the staged bundle before constructing one `data` commit, keeping
the source checkout and index untouched.
