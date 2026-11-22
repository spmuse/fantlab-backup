# FantLab Backup

Back up a FantLab user's ratings and reviews to dated, verifiable snapshots.

FantLab has no export button, so this script fetches the public ratings list
and the public reviews list and stores them as JSON and CSV, together with the
raw HTML. Each run checks its own counts against the totals FantLab reports,
so a bad run fails loudly instead of writing a bad snapshot.

## What gets backed up

- Every rating: work, authors, type, year, score, date rated, and whether a
  review exists.
- Every published review, with its full text, date, and score.
- A diff against the previous snapshot: new, changed, and removed ratings
  and reviews.

Unpublished review drafts are not included: they live behind login and are
not part of the public lists.

## Requirements

- Python 3.12, standard library only. No third-party packages, no sign-in.

## Usage

```sh
python3 scripts/fantlab_backup.py --user-id 54173 --username webstranger --dry-run   # fetch and verify, write nothing
python3 scripts/fantlab_backup.py --user-id 54173 --username webstranger             # write a dated snapshot
```

`--user-id` is the numeric id in the profile URL
(`https://fantlab.ru/user54173` → id `54173`) and is required;
`--username` is the FantLab login, used only for display and for matching
the review byline, and is optional. No user is hard-coded in the script:
anyone can back up their own account by passing their own id.

Snapshots go under `snapshots/<YYYY-MM-DD>/` and contain:

- `ratings.json` and `ratings.csv` — all ratings
- `reviews.json` — all reviews with full text
- `profile.json` — the totals reported by FantLab at backup time
- `diff.json` — new, changed, and removed items since the previous snapshot
- `raw/` — the fetched HTML, for re-parsing later

Exit codes: 0 ok, 1 fetch or parse failure, 2 verification mismatch
(collected counts do not match FantLab's totals). On a mismatch nothing is
written to a snapshot directory; diagnostics go to `snapshots/failed/`.

## Scheduling

A weekly run is enough for most readers. Example cron entry, Mondays at
09:19:

```cron
19 9 * * 1 python3 /path/to/fantlab-backup/scripts/fantlab_backup.py --user-id 54173 --username webstranger
```

## How it works

- Ratings come from `https://fantlab.ru/user<id>/marks`, 200 rows per page,
  sorted by date (`sort=date`).
- Reviews come from `https://fantlab.ru/user<id>/responses`, 50 per page,
  with the full text inline in the list.
- Requests are paced at about one per 1.2 seconds, with retries for
  transient errors, and the run stops if the site starts returning errors.

## License

MIT. See `LICENSE`.
