"""Offline native-runtime measurements and hash-bound release evidence.

This stdlib-only module never inspects user databases or installs dependencies.
The release owner supplies the reviewed policy digest independently of the
policy file; a matching digest establishes integrity, not vendor approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ATTESTATION_SCHEMA = "neocortex.sqlite-runtime-attestation/v1"
POLICY_SCHEMA = "neocortex.sqlite-runtime-policy/v1"
NATIVE_RUNTIME_SCHEMA = "neocortex.native-runtime/v1"
POLICY_FILENAME = "sqlite-runtime-policy.json"
CAPABILITIES = ("fts5", "json", "foreign_keys", "rollback", "wal_full")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class SQLiteAttestationError(ValueError):
    """A probe or a hash-bound offline policy could not be validated."""


# Execute this code in the release interpreter; importing this helper in the
# build interpreter must not accidentally measure the build interpreter.
_PROBE_SOURCE = r'''
import _sqlite3
import hashlib
import json
import os
import platform
import sqlite3
import stat
import sys
from pathlib import Path


def file_identity(path, expected_mapping=None):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("runtime artifact is not a regular file")
        if expected_mapping is not None:
            major, minor, inode = expected_mapping
            if (os.major(before.st_dev), os.minor(before.st_dev), before.st_ino) != (major, minor, inode):
                raise RuntimeError("loaded SQLite library differs from its mapped file")
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
    latest = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(latest, key) for key in fields):
        raise RuntimeError("runtime artifact changed while hashing")
    return {"path": str(path), "sha256": digest, "size": before.st_size}


def loaded_runtime_libraries(fragment):
    maps = Path("/proc/self/maps")
    if not maps.is_file():
        return {"mode": "static_or_unobserved", "reason": "process_maps_unavailable", "files": []}
    paths = {}
    for line in maps.read_text(encoding="utf-8").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or fragment not in Path(fields[5]).name:
            continue
        path = fields[5]
        if path.endswith(" (deleted)"):
            raise RuntimeError("loaded SQLite library was deleted or replaced")
        major, minor = (int(part, 16) for part in fields[3].split(":"))
        paths[path] = (major, minor, int(fields[4]))
    if not paths:
        return {"mode": "static_or_unobserved", "reason": "no_separate_" + fragment + "_mapping", "files": []}
    return {"mode": "process_maps", "reason": None,
            "files": [file_identity(path, mapping) for path, mapping in sorted(paths.items())]}


def executable_identity():
    mapped = Path("/proc/self/exe").stat()
    return file_identity(sys.executable, (os.major(mapped.st_dev), os.minor(mapped.st_dev), mapped.st_ino))


def sqlite_module_identity():
    module_path = getattr(_sqlite3, "__file__", None)
    if module_path is not None:
        mapped = loaded_runtime_libraries("_sqlite3")
        resolved = str(Path(module_path).resolve(strict=True))
        matches = [item for item in mapped["files"] if item["path"] == resolved]
        if len(matches) != 1:
            raise RuntimeError("loaded SQLite extension cannot be bound to its mapped binary")
        return {"kind": "extension", **matches[0]}
    if _sqlite3.__spec__.origin != "built-in":
        raise RuntimeError("SQLite module has neither a binary path nor a builtin identity")
    # Standalone CPython can compile _sqlite3 into its executable. Its exact
    # executable bytes then bind the module as well as the interpreter.
    return {"kind": "builtin", **executable_identity(),
            "container_libraries": loaded_runtime_libraries("libpython")["files"]}


caps = {}
errors = {}
connection = sqlite3.connect(":memory:")
try:
    version, source_id = connection.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()
    compile_options = sorted(row[0] for row in connection.execute("PRAGMA compile_options"))
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.probe_fts USING fts5(value)")
        connection.execute("INSERT INTO probe_fts VALUES ('neocortex probe')")
        caps["fts5"] = connection.execute("SELECT count(*) FROM probe_fts WHERE probe_fts MATCH 'neocortex'").fetchone()[0] == 1
        connection.commit()
    except sqlite3.Error as exc:
        caps["fts5"] = False
        errors["fts5"] = str(exc)
        connection.rollback()
    try:
        caps["json"] = connection.execute("SELECT json_extract('{\"probe\":7}', '$.probe')").fetchone()[0] == 7
    except sqlite3.Error as exc:
        caps["json"] = False
        errors["json"] = str(exc)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TEMP TABLE probe_parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TEMP TABLE probe_child(id INTEGER REFERENCES probe_parent(id))")
        enforced = False
        try:
            connection.execute("INSERT INTO probe_child VALUES (97)")
        except sqlite3.IntegrityError:
            enforced = True
        connection.rollback()
        caps["foreign_keys"] = enforced and connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    except sqlite3.Error as exc:
        caps["foreign_keys"] = False
        errors["foreign_keys"] = str(exc)
        connection.rollback()
    try:
        connection.execute("CREATE TEMP TABLE probe_rollback(value INTEGER)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO probe_rollback VALUES (1)")
        connection.rollback()
        caps["rollback"] = connection.execute("SELECT count(*) FROM probe_rollback").fetchone()[0] == 0
    except sqlite3.Error as exc:
        caps["rollback"] = False
        errors["rollback"] = str(exc)
        connection.rollback()
finally:
    connection.close()

wal = sqlite3.connect(str(Path(sys.argv[1]) / "probe.sqlite3"))
try:
    try:
        mode = wal.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        wal.execute("PRAGMA synchronous=FULL")
        synchronous = wal.execute("PRAGMA synchronous").fetchone()[0]
        wal.execute("CREATE TABLE probe(value INTEGER)")
        wal.execute("INSERT INTO probe VALUES (19)")
        wal.commit()
        caps["wal_full"] = mode == "wal" and synchronous == 2 and wal.execute("SELECT value FROM probe").fetchone()[0] == 19
    except sqlite3.Error as exc:
        caps["wal_full"] = False
        errors["wal_full"] = str(exc)
finally:
    wal.close()

result = {
    "schema": "neocortex.sqlite-runtime-attestation/v1",
    "python": {"implementation": sys.implementation.name, "version": platform.python_version(),
               "cache_tag": sys.implementation.cache_tag, "invoked_executable": sys.executable,
               "executable": executable_identity()},
    "sqlite": {"version": version, "source_id": source_id, "compile_options": compile_options,
               "module": sqlite_module_identity(), "native_libraries": loaded_runtime_libraries("libsqlite3")},
    "capabilities": caps, "capability_errors": errors,
    "limitations": ["wal_full verifies local configuration and a commit, not power-loss durability",
                    "runtime identity does not itself establish vendor patch approval"]
}
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''


def canonical_sha256(value: object) -> str:
    """Hash canonical UTF-8 JSON, not the incidental formatting of a file."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _artifact_hash(value: Any) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("sha256"), str):
        raise SQLiteAttestationError("runtime artifact hash is missing")
    digest = value["sha256"]
    if not _SHA256.fullmatch(digest):
        raise SQLiteAttestationError("runtime artifact hash is invalid")
    return digest


def runtime_identity(attestation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the path-independent identity that survives staging publication."""
    if attestation.get("schema") != ATTESTATION_SCHEMA:
        raise SQLiteAttestationError("unsupported SQLite attestation schema")
    try:
        python = attestation["python"]
        sqlite = attestation["sqlite"]
        native = sqlite["native_libraries"]
        for value in (python["implementation"], python["version"], python["cache_tag"],
                      sqlite["version"], sqlite["source_id"]):
            if not isinstance(value, str) or not value:
                raise SQLiteAttestationError("runtime identity string is missing")
        options = sqlite["compile_options"]
        if not isinstance(options, list) or any(not isinstance(item, str) for item in options):
            raise SQLiteAttestationError("invalid SQLite compile options")
        if native["mode"] not in {"process_maps", "static_or_unobserved"}:
            raise SQLiteAttestationError("invalid native-library observation mode")
        files = native["files"]
        if not isinstance(files, list) or (native["mode"] == "process_maps") != bool(files):
            raise SQLiteAttestationError("inconsistent native-library observation")
        return {
            "implementation": python["implementation"], "python_version": python["version"],
            "cache_tag": python["cache_tag"], "python_sha256": _artifact_hash(python["executable"]),
            "sqlite_version": sqlite["version"], "sqlite_source_id": sqlite["source_id"],
            "sqlite_module_sha256": _artifact_hash(sqlite["module"]),
            "sqlite_module_kind": sqlite["module"].get("kind", "extension"),
            "sqlite_builtin_container_sha256": sorted({
                _artifact_hash(item) for item in sqlite["module"].get("container_libraries", [])
            }),
            "compile_options": sorted(set(options)), "native_mode": native["mode"],
            "native_sha256": sorted({_artifact_hash(item) for item in files}),
        }
    except (KeyError, TypeError) as exc:
        raise SQLiteAttestationError("incomplete SQLite runtime identity") from exc


def collect_release_sqlite_attestation(release_root: Path, *, timeout: float = 60) -> dict[str, Any]:
    """Measure only release_root/bin/python in an owned temporary directory.

    Existing release-tree/symlink validation remains the release owner's job.
    Isolated Python ignores ambient PYTHONPATH; no state/corpus path is accepted.
    """
    root = Path(release_root).absolute()
    python = root / "bin" / "python"
    try:
        root_mode = root.lstat().st_mode
        python_mode = python.stat().st_mode
    except OSError as exc:
        raise SQLiteAttestationError("release interpreter is unavailable") from exc
    if not stat.S_ISDIR(root_mode) or not stat.S_ISREG(python_mode) or not os.access(python, os.X_OK):
        raise SQLiteAttestationError("release interpreter is not an executable in a real release root")
    with tempfile.TemporaryDirectory(prefix="neocortex-sqlite-probe-") as directory:
        environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": directory, "TMPDIR": directory, "TMP": directory, "TEMP": directory,
            "XDG_CONFIG_HOME": directory, "XDG_CACHE_HOME": directory,
            "XDG_DATA_HOME": directory, "XDG_STATE_HOME": directory,
        }
        try:
            result = subprocess.run(
                (str(python), "-I", "-c", _PROBE_SOURCE, directory),
                cwd=directory, env=environment, text=True, capture_output=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SQLiteAttestationError("release SQLite probe could not execute") from exc
        if result.returncode != 0:
            raise SQLiteAttestationError(f"release SQLite probe failed: {result.stderr[-2000:]}")
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise SQLiteAttestationError("release SQLite probe returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise SQLiteAttestationError("release SQLite probe returned a non-object")
        try:
            invoked = payload["python"]["invoked_executable"]
        except (KeyError, TypeError) as exc:
            raise SQLiteAttestationError("release interpreter identity is missing") from exc
        if not isinstance(invoked, str) or os.path.abspath(invoked) != str(python):
            raise SQLiteAttestationError("probe did not run as release bin/python")
        identity = runtime_identity(payload)
        payload["identity_sha256"] = canonical_sha256(identity)
        payload["probe_sha256"] = hashlib.sha256(_PROBE_SOURCE.encode("utf-8")).hexdigest()
        return payload


def validate_sqlite_policy(
    policy: Mapping[str, Any], *, expected_policy_sha256: str,
) -> str:
    """Validate every approval entry before a runtime or product path is touched."""
    digest = canonical_sha256(policy)
    if not isinstance(expected_policy_sha256, str) or not _SHA256.fullmatch(expected_policy_sha256) or digest != expected_policy_sha256:
        raise SQLiteAttestationError("SQLite runtime policy digest differs from its trusted binding")
    if policy.get("schema") != POLICY_SCHEMA or not isinstance(policy.get("policy_id"), str) or not policy["policy_id"]:
        raise SQLiteAttestationError("SQLite runtime policy identity is invalid")
    required = policy.get("required_capabilities")
    if not isinstance(required, list) or set(required) != set(CAPABILITIES) or len(required) != len(CAPABILITIES):
        raise SQLiteAttestationError("SQLite runtime policy must require every product capability")
    approvals = policy.get("approved_builds")
    if not isinstance(approvals, list):
        raise SQLiteAttestationError("SQLite runtime approval set is invalid")
    for entry in approvals:
        if not isinstance(entry, dict) or not isinstance(entry.get("identity_sha256"), str) or not _SHA256.fullmatch(entry["identity_sha256"]):
            raise SQLiteAttestationError("SQLite runtime approval identity is invalid")
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("basis") not in {"upstream", "vendor_backport"}:
            raise SQLiteAttestationError("SQLite runtime approval evidence is missing")
        for field in ("provider", "build_reference", "reviewed_on", "source_url"):
            if not isinstance(evidence.get(field), str) or not evidence[field]:
                raise SQLiteAttestationError("SQLite runtime approval evidence is incomplete")
        if not evidence["source_url"].startswith("https://") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", evidence["reviewed_on"]):
            raise SQLiteAttestationError("SQLite runtime approval review metadata is invalid")
    return digest


def evaluate_sqlite_runtime(
    attestation: Mapping[str, Any], policy: Mapping[str, Any], *, expected_policy_sha256: str,
) -> dict[str, Any]:
    """Evaluate an explicitly pinned policy; no version-only approval exists.

    Approval entries bind the complete path-independent runtime identity, with
    reviewed evidence. An empty approval set is a valid unaccredited policy.
    The expected policy digest must come from the release owner's trusted input,
    not be accepted from the same untrusted file it is intended to verify.
    """
    digest = validate_sqlite_policy(policy, expected_policy_sha256=expected_policy_sha256)
    required = policy["required_capabilities"]
    approvals = policy["approved_builds"]
    identity_sha256 = canonical_sha256(runtime_identity(attestation))
    if attestation.get("identity_sha256") != identity_sha256:
        raise SQLiteAttestationError("SQLite runtime attestation identity digest is inconsistent")
    capabilities = attestation.get("capabilities")
    if not isinstance(capabilities, dict):
        raise SQLiteAttestationError("SQLite runtime capabilities are missing")
    missing = [name for name in required if capabilities.get(name) is not True]
    matches = [entry for entry in approvals if entry["identity_sha256"] == identity_sha256]
    status = "incompatible" if missing else "approved" if matches else "unaccredited"
    return {
        "status": status, "policy_id": policy["policy_id"], "policy_sha256": digest,
        "identity_sha256": identity_sha256, "missing_capabilities": missing,
        "approval_evidence": matches[0]["evidence"] if status == "approved" else None,
    }


def _read_regular_evidence(path: Path, *, label: str, maximum: int = 2_000_000) -> bytes:
    """Reject special files before reading and retain the opened file identity."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise SQLiteAttestationError(f"{label} is not a bounded regular file")
        raw = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        named = path.lstat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if len(raw) > maximum:
        raise SQLiteAttestationError(f"{label} exceeds its byte limit")
    if not stat.S_ISREG(named.st_mode) or any(
        getattr(before, name) != getattr(observed, name)
        for observed in (after, named) for name in fields
    ):
        raise SQLiteAttestationError(f"{label} changed while reading")
    return raw


def read_sqlite_policy(path: Path, *, expected_policy_sha256: str) -> dict[str, Any]:
    """Read a bounded regular policy file using an independently supplied pin."""
    try:
        payload = json.loads(_read_regular_evidence(path, label="SQLite runtime policy"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SQLiteAttestationError("SQLite runtime policy is unavailable or malformed") from exc
    if not isinstance(payload, dict):
        raise SQLiteAttestationError("SQLite runtime policy is not an object")
    validate_sqlite_policy(payload, expected_policy_sha256=expected_policy_sha256)
    return payload


def native_runtime_record(
    attestation: Mapping[str, Any], policy: Mapping[str, Any], *, expected_policy_sha256: str,
) -> dict[str, Any]:
    """Bind the full measurement and the evaluated, independently pinned policy."""
    decision = evaluate_sqlite_runtime(
        attestation, policy, expected_policy_sha256=expected_policy_sha256,
    )
    probe_digest = attestation.get("probe_sha256")
    if not isinstance(probe_digest, str) or not _SHA256.fullmatch(probe_digest):
        raise SQLiteAttestationError("SQLite attestation probe hash is invalid")
    return {
        "schema": NATIVE_RUNTIME_SCHEMA,
        "attestation": dict(attestation),
        "attestation_sha256": canonical_sha256(attestation),
        "identity_sha256": decision["identity_sha256"],
        "probe_sha256": probe_digest,
        "policy_id": policy["policy_id"],
        "policy_sha256": decision["policy_sha256"],
        "decision": decision,
    }


def validate_native_runtime_record(
    record: object, policy: Mapping[str, Any], *, expected_policy_sha256: str,
) -> dict[str, Any]:
    """Reject incomplete v2 evidence; callers must bind the record to a receipt."""
    if not isinstance(record, dict) or record.get("schema") != NATIVE_RUNTIME_SCHEMA:
        raise SQLiteAttestationError("native runtime record schema is invalid")
    attestation = record.get("attestation")
    if not isinstance(attestation, dict):
        raise SQLiteAttestationError("native runtime attestation is missing")
    expected = native_runtime_record(
        attestation, policy, expected_policy_sha256=expected_policy_sha256,
    )
    if record != expected:
        raise SQLiteAttestationError("native runtime record differs from its bound evidence")
    return expected


def observe_platform_native_runtime(
    release_root: Path | None = None, *, receipts_directory: Path | None = None,
    probe_timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Report current measurements separately from immutable stored evidence.

    Reading a manifest alone never reports approval. An ordinary venv or a v1
    release remains unaccredited, even when every capability works.
    """
    root = Path(sys.prefix) if release_root is None else Path(release_root)
    try:
        observed = collect_release_sqlite_attestation(root, timeout=probe_timeout_seconds)
        report: dict[str, Any] = {
            "status": "legacy_unaccredited", "measurement": "isolated_release_interpreter",
            "observed": observed, "stored": None,
        }
        manifest_path = root / "neocortex-release.json"
        try:
            manifest_bytes = _read_regular_evidence(manifest_path, label="release manifest")
        except FileNotFoundError:
            return report
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise SQLiteAttestationError("release manifest is not an object")
        version = manifest.get("schema_version")
        if type(version) is int and version == 1:
            return report
        if type(version) is not int or version != 2:
            raise SQLiteAttestationError("release manifest schema is unsupported")
        stored = manifest.get("native_runtime")
        if not isinstance(stored, dict):
            raise SQLiteAttestationError("native runtime evidence is missing")
        if manifest.get("native_runtime_sha256") != canonical_sha256(stored):
            raise SQLiteAttestationError("native runtime manifest digest differs")
        pin = stored.get("policy_sha256")
        if not isinstance(pin, str):
            raise SQLiteAttestationError("native runtime policy pin is missing")
        policy = read_sqlite_policy(root / POLICY_FILENAME, expected_policy_sha256=pin)
        validate_native_runtime_record(stored, policy, expected_policy_sha256=pin)
        decision = evaluate_sqlite_runtime(observed, policy, expected_policy_sha256=pin)
        report.update({"stored": stored, "decision": decision, "status": decision["status"]})
        # A locally created manifest and policy must not authenticate themselves.
        # The immutable manifest was bound to this separate installation receipt
        # at activation. This is local provenance, not a cryptographic signature.
        receipts = [] if receipts_directory is None else sorted(
            receipts_directory.glob("*.json"), reverse=True,
        )
        if not receipts:
            report.update({"status": "unaccredited", "reason": "installation_receipt_unavailable"})
        else:
            receipt = json.loads(_read_regular_evidence(receipts[0], label="installation receipt"))
            artifacts = receipt.get("artifacts") if isinstance(receipt, dict) else None
            if (
                not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
                or receipt["schema_version"] != 2 or receipt.get("kind") != "linux_release_receipt"
                or receipt.get("result") != "success" or receipt.get("release_path") != str(root)
                or receipt.get("release_id") != root.name
                or receipt.get("source_sha") != manifest.get("source_sha")
                or receipt.get("native_runtime") != stored
                or receipt.get("native_runtime_sha256") != manifest["native_runtime_sha256"]
                or not isinstance(artifacts, dict)
                or artifacts.get("native_runtime") != stored
                or artifacts.get("native_runtime_sha256") != manifest["native_runtime_sha256"]
                or artifacts.get("release_manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            ):
                report.update({"status": "unaccredited", "reason": "installation_receipt_binding_failed"})
        if observed["identity_sha256"] != stored["identity_sha256"]:
            report.update({"status": "unaccredited", "reason": "native_runtime_identity_changed"})
        if any(os.environ.get(name) for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")):
            report.update({"status": "unaccredited", "reason": "ambient_native_loader_override"})
        return report
    except (SQLiteAttestationError, OSError, UnicodeError, ValueError) as exc:
        return {"status": "unaccredited", "measurement": "failed", "reason": str(exc)}
