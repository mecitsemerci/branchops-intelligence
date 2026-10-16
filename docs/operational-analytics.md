# Operational analytics

Built from the stored incident data only. The functions live in `application/queries/operations.py` and are
served by `GET /api/analytics/operations?days=90` (VIEWER) and the "Operasyonel eğilimler" section of the
analytics page. Every number is a parameterised SQL aggregate that honours the caller's scope.

| Metric | Definition |
|---|---|
| Application / branch / category trends | records and average quality/completeness per ISO week, for the 8 most frequent values in the period |
| Missing fields per category | template fields most often confirmed `MISSING`, with `UNKNOWN` counted separately (latest analysis per record) |
| Quality after updates | records re-analysed after a source update: improved / unchanged / worsened (±0.5 points), mean delta, median hours between first and latest analysis (provisional analyses from model outages excluded) |
| Recurring problems | same error signature, already part of the quality analytics (`recurring`) |
| Reopened records | records whose source status moved from a resolved state back to an open state, and the reopen rate among resolved records |
| Recurrence after resolution | new records with the same error signature created within 30 days after a record with that signature was resolved |

## Status history

* Each accepted change of the source status writes a row to `issue_status_events` (from, to, resolved, source
  time) and keeps `issues.resolved_at` up to date. The snapshot merge accepts only newer values, so an
  out-of-order (older) webhook never produces a transition.
* Which statuses count as resolved is configured in `analytics.resolved_statuses` (case-insensitive
  substrings). The default covers "Çözüldü", "Kapatıldı", "Kapalı", "Tamamlandı", "Resolved", "Closed",
  "Done", "Cancelled" and "İptal". Adjust it to the bank's Jira and ServiceNow workflows.
* History starts with this release. Older records have no transitions until their status changes again.

## What is never fabricated

* When no resolved status has been seen, reopen and recurrence-after-resolution are returned as `"UNKNOWN"`,
  with `coverage` (records with status history, resolved records) and a Turkish explanation. They are never
  returned as 0.
* Counts are counts of records. They are **not** a proxy for business impact: no customer counts, transaction
  volumes or amounts are derived or estimated, and every response carries this note.
* Model-written summaries (`branchops report`) only rephrase these SQL figures.
