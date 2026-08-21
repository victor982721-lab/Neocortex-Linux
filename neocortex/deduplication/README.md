# Deduplication

`neocortex.deduplication` owns immutable domain values, native path I/O, stable
file snapshots, XXH3 fingerprinting, inventory policy and scanning, SQLite
schema lifecycle and non-destructive duplicate planning.

The pipeline reduces candidates by exact size, optionally uses a sampled
fingerprint for large files, computes full fingerprints for remaining
collisions and verifies byte equality before publishing duplicate groups. It
persists only complete, policy-bound inventory generations and validates file
identity again whenever content is read.

```python
from neocortex.deduplication import DedupIndex, DedupPlanner

with DedupIndex("state/dedup.sqlite3") as index:
    scan = index.scan("corpus")
    plan = DedupPlanner(index).plan(scan.scan_id)
```

This boundary plans only; corpus mutation remains an explicitly authorized
higher-level responsibility.
