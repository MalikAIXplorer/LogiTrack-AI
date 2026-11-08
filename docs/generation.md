# Streaming generation and feature output

## Generation

`generate_dataset()` builds one shipment chunk at a time. Each shipment's random values come from a BLAKE2-derived seed using the configured seed, the `shipment` namespace, and its stable shipment number. This keeps operational values and IDs the same when the batch size changes. Master records and calendars are emitted once; the remaining batches retain only the small route/disruption lookup catalogs and the current chunk.

SQLite writes happen in parent-before-child order inside a transaction per chunk, with foreign-key enforcement enabled. CSV writers stay open and append rows as each chunk is persisted. Delay reason IDs are assigned from a run-wide counter so they remain stable across batch sizes. Delivery labels are still derived from actual versus planned delivery and the service-level tolerance. The four anomaly scenarios are inserted as separate audit/evidence records with ground-truth annotations in their separate output.

Generation uses a sibling staging directory. It validates SQLite and CSV output before copying records to the requested destination. The source manifest is published last by atomic rename and marks a complete bundle. The batch size must be at least four so the designated anomaly evidence can be assembled in the first batch. On ordinary exceptions, staging and partial publication are removed. A process interruption can leave an unmarked destination or staging directory; those are not valid bundles and must be removed before a clean restart. Resume is not implemented.

The generator refuses to write to `data/starter` or to an existing nonempty output directory. It can publish into a new path, or an existing empty directory such as a pytest fixture directory.

## Validation

`validate_dataset()` reads CSV rows one at a time. It stages primary keys and ground-truth label fields in temporary SQLite tables, detects duplicates with primary-key constraints, compares CSV and database key sets with SQL `EXCEPT`, and checks CSV foreign keys against indexed SQLite parent keys. Delivery labels and anomaly links are checked with SQL. This avoids retaining complete table exports or ID sets in Python memory.

## Feature output

The production feature writer iterates ETA snapshots and paired targets, writing each row immediately. Event queries require both `event_time <= predicted_at` and `ingestion_time <= predicted_at`. A disk-backed temporary queue index lets each snapshot retrieve the latest queue reading for each route facility without scanning its full queue history. Actual delivery is read only by the target iterator. Ground truth, audit tables, and anomaly evidence are not feature sources. Summary counts and missing-value counts are accumulated as counters. List-returning helper functions remain for small tests and interactive use.

## Restart and output policy

Runs are restart-from-scratch only. Reusing an existing complete output path is rejected; the caller should choose a new path or explicitly remove the old generated output before retrying. The validated starter output is never a generator destination.
