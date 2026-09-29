# CredoWeb CSV tables

Start with **profiles.csv**: one row per CredoWeb profile. The scraper refreshes
these CSV files periodically in place while it collects data, and once more when
the run finishes or stops. The file names stay the same; no archive is required.
Use **manifest.json** for the published snapshot time, counts and collection
status. A successful CSV refresh does not mean that collection has finished.

## Files and relationships

| File | One row represents | Join |
| --- | --- | --- |
| profiles.csv | One profile with its first available contact and address | Unique profile_id; cross-source key entity_key |
| workplaces.csv | One distinct workplace or facility location | profile_id |
| contacts.csv | One published phone, email or website in its profile/workplace context | profile_id; optional workplace_key |
| specialties.csv | One main or other specialty | profile_id |

**schema.json** describes the columns, types and table keys. Source labels remain
in their original language; column names use English snake_case.

## Import settings

- Format: standard comma-delimited CSV, quoted when needed, UTF-8 with BOM and
  CRLF row endings. CSV does not store column types.
- Import IDs, phone numbers and postcodes as **Text**. In Excel use
  **Data -> From Text/CSV**, select comma and disable automatic numeric conversion
  for these columns. Treat source text as text, not spreadsheet formulas.
- Phone strings retain leading zeros and plus signs. No Excel-specific apostrophe
  prefixes are added to the data. Original values remain in contact_value.
- An empty field means unknown/unavailable. It is not zero or proof that a contact
  does not exist. Boolean fields use true, false or an empty value for unknown.

## Merge rules

1. Match repeated CredoWeb snapshots on profile_id or entity_key
   (credoweb:bg:<profile_id>). Do not identify people by name alone.
2. Join each child table on profile_id. A facility_profile_id may identify a
   facility outside the collected directory, so use a left join for that link.
3. workplace_key and contact_id are deterministic from the available data and
   can change when a location or contact is enriched. Replace the child rows for
   each affected profile when loading a newer snapshot.
4. name_normalized only casefolds and collapses whitespace. contact_normalized
   is a matching aid, not a unique person identifier. Shared numbers and emails
   need review before merging people across different sources.
5. profile_city describes the profile. Address fields and workplaces describe a
   practice/facility location, which can be in a different city.

The primary phone, email, website, specialty and address columns show the first
available value, not a recommended contact or principal practice. Additional
values remain in the child tables. Profile phone/email counts represent distinct
original values, so contact-row counts can differ across workplaces.

Phone normalization removes formatting only from unambiguous single numbers
with at most 15 digits. It does not guess country codes or split ambiguous
strings. Explicit source codes remain in phone_country_code and an explicit
source preference flag remains in is_primary. The normalized number does not
automatically prepend the separate code. Emails are trimmed and lowercased;
websites are trimmed. Conflicting known workplace roles, dates or flags remain
separate relationships.

## Progress and provenance

record_status retains collection progress: listed has directory data,
in_progress has some fetched sections, complete has all advertised sections
processed, and partial has missing/failed sections. Blank contact/address fields
can still be awaiting collection. partial_snapshot describes record completeness;
consult the collection manifest for directory discovery coverage.

source_url links to each profile. source_path identifies the source field for a
workplace or contact. detail_fetched_at is a saved profile timestamp, not a new
verification date for cached data. The tables focus on identity, contacts,
locations and specialties. Full descriptions and other raw fields remain in the
scraper's full exports and saved source records.

## Reading during an update

Files are staged first and each file is replaced atomically. **manifest.json is
published last**. Its files entries include CSV row counts and SHA-256 hashes;
support_files contains the schema and README hashes. Readers that need a
consistent multi-file snapshot should read the manifest, load and verify the
files, and check that the manifest has not changed. Retry if a hash differs or
the manifest changes. A locked file can delay publication; the scraper retries.

## Optional standalone refresh

The scraper handles regular updates. To regenerate these files from a saved
checkpoint without fetching websites, run from the scraper folder:

```powershell
python .\credoweb_merge.py --database output/checkpoint.sqlite3 --output output/merge
```

The standalone command reads a consistent in-memory SQLite snapshot and closes
the source connection before transforming data. It does not stop the collector.
