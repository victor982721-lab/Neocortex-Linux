# `neocortex.enumeration`

Canonical filesystem-enumeration boundary, split by responsibility:

- `models.py` and `errors.py` define the portable data and error contracts;
- `ntfs/` preserves the legacy Windows-only MFT/USN parser, volume backend,
  enumerator and journal reader without extending platform support;
- `path_index/` owns the optional SQLite schema and repository used to resolve
  NTFS file-reference relationships.

The public package resolves symbols lazily. The numbered legacy root remains
only as a compatibility facade during consumer migration.
