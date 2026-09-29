# Weekly collection and CSV integration

The source code lives in [ChocoRamaBG/credoweb-scraper](https://github.com/ChocoRamaBG/credoweb-scraper).
The [data branch](https://github.com/ChocoRamaBG/credoweb-scraper/tree/data) contains the latest
published CSV snapshot at fixed paths:

| File | Contents | Join key |
| --- | --- | --- |
| `profiles.csv` | One row per known profile, including separate phone/email columns and available full addresses | `profile_id` or `entity_key` |
| `workplaces.csv` | Individual workplaces, facilities and their addresses | `profile_id`; optional `facility_profile_id` links to another profile |
| `contacts.csv` | Individual phones, emails and websites | `profile_id`; optional `workplace_key` |
| `specialties.csv` | Profile specialties | `profile_id` |
| `schema.json` | Column definitions and types | — |
| `manifest.json` | Snapshot time, row counts, collection status and file checksums | — |
| `README.md` | Detailed merge and import rules | — |
| `full/profiles.csv.gz` | Detailed Bulgarian profile export, including descriptions and source evidence | `ID` |
| `full/workplaces.csv.gz` | Detailed workplace occurrences, retaining raw JSON and observed coordinates | `ID профил` |
| `full/manifest.json` | Detailed CSV schema, compressed/uncompressed checksums and normalized snapshot link | — |

CSV uses comma separators, UTF-8 with a BOM, and quoted fields where needed.
Treat identifiers, phone numbers and postal codes as text. Empty values mean the
source did not supply a value. A profile's presence does not imply all of its
detail sections have been collected: check `record_status` and the manifest.
`detail_fetched_at` is the latest detail collection attempt time. Partial or
in-progress records can retain older successful contacts and addresses while a
refresh is incomplete; that timestamp does not mean every field was refreshed.

The `full/` files use the original semicolon-delimited Bulgarian CSV format,
compressed without changing its bytes. They retain fields omitted by the
normalized tables. See the [detailed publication contract](full-publication.md)
before importing; both formats are published in the same immutable Git commit.

## Schedule and persistence

`Weekly CredoWeb refresh` runs every Monday at **03:17 UTC** (06:17 during Bulgarian
summer time, 05:17 during winter time). It can also be started from the repository's
[Actions page](https://github.com/ChocoRamaBG/credoweb-scraper/actions/workflows/weekly-refresh.yml)
using **Run workflow**.

Each run restores the previous SQLite checkpoint, discovers profiles, refreshes
older information and continues detail collection. Known profiles are retained
when a search pass is incomplete. Missing sections are enriched across runs.
The default collection budget is four hours, with the first 30 minutes reserved
as the maximum discovery budget. Reaching the time budget saves progress and
publishes a partial snapshot; it does not claim the entire directory was refreshed.
The job does not need this computer to stay switched on.

Successful responses from an unfinished refresh pass remain reusable during the
next run. This allows large searches and multi-page profiles to make progress
instead of starting again each week. Older complete profiles become eligible
for refresh after six days, with work spread across runs.
Long profiles receive up to two minutes per visit before the collector rotates
to other profiles; their successfully downloaded pages are kept for the next visit.

SQLite is stored as compressed release assets under the `checkpoint` release,
separately from Git history. The workflow retains recent checkpoints and restores
the newest valid published checkpoint on the next run. The release is internal
state; downstream scripts should use the CSV files. The workflow verifies files
before publishing all CSV changes in one `data` branch commit.

GitHub's scheduler runs from the default branch and may start later than the cron
time. The repository must keep Actions enabled. See GitHub's
[schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Read from another GitHub workflow

Check out the `data` branch into a separate directory. This gives all CSV files
from the same commit, avoiding a mixture of snapshots during a concurrent update.
Use this scraper's repository as the source:

```yaml
- name: Load CredoWeb CSV snapshot
  uses: actions/checkout@v7
  with:
    repository: ChocoRamaBG/credoweb-scraper
    ref: data
    path: credoweb-data
    token: ${{ secrets.CREDOWEB_READ_TOKEN }}
    persist-credentials: false

- name: Read profiles
  run: python your_merge_script.py credoweb-data/profiles.csv
```

For a private source repository, `CREDOWEB_READ_TOKEN` must have read access to
that repository. The default token of a different repository cannot access it.
For a public source repository, omit the `token` line. Do not embed tokens in
scripts, CSV files or URLs.

Python example without additional packages:

```python
import csv
from pathlib import Path

snapshot = Path("credoweb-data")
with (snapshot / "profiles.csv").open(encoding="utf-8-sig", newline="") as stream:
    profiles = {row["entity_key"]: row for row in csv.DictReader(stream)}

with (snapshot / "workplaces.csv").open(encoding="utf-8-sig", newline="") as stream:
    for workplace in csv.DictReader(stream):
        profile = profiles.get("credoweb:bg:" + workplace["profile_id"])
        # Merge this workplace with its parent profile in your destination.
```

Upsert master rows by `entity_key`. Replace a profile's related workplace, contact
and specialty rows from the current snapshot instead of appending duplicates.
Child keys can change when new source fields enrich a record. Keep source IDs
alongside any IDs from other datasets; a name alone is not a reliable join key.

The `data` commit SHA identifies an immutable snapshot. Store it with your import
log. `manifest.json` includes SHA-256 hashes if you download files individually.

## Run the same update locally

```sh
python credoweb_scraper.py --output output --refresh-days 6 --max-runtime-seconds 14400 --discovery-budget-seconds 1800 --quick-export
```

The ordinary scraper command still produces `output/merge/*.csv` automatically.
The weekly options add refresh and time limits to the existing resumable scraper.

## Failure handling

An API error retains prior successful data for retry. A missing or corrupt saved
checkpoint after publication stops the job rather than replacing the database
with an empty collection. Export validation failures prevent CSV publication.
Check the Actions run summary and collection status to distinguish an expected
budget stop or incomplete directory coverage from a failed job.

The weekly job's GitHub token can write only within this repository. It does not
notify or run scripts in other repositories. A downstream workflow can pull the
latest data on its own schedule; cross-repository dispatch can be added when the
integration target is known.
# Automatic hospital-map publication

After `Weekly CredoWeb refresh` succeeds on `master`, the separate
`hospital-map-source-update.yml` workflow notifies the configured local Windows
runner. It executes the installed hospital-map bridge, without checking out or
executing scraper code on that computer. The bridge compares the detailed CSV
file contents on `data` with the last successfully published inputs. Changed
snapshots pass through the existing matching, manual-correction and website
publication pipeline; unchanged CSVs do not request another rebuild.

The local computer must be on and signed in for that final step. The website
continues using its existing local-network access controls. This adds no timed
polling of GitHub. A manually edited `data` branch can be imported by running
`Update hospital map from changed source files` from the Actions tab on `master`;
the data-only branch deliberately does not contain executable workflows.
