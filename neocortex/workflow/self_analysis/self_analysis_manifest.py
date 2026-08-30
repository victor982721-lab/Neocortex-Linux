"""Pure bounded codec for durable self-analysis manifests.

Historical evidence is validated lexically only.  This module deliberately has
no SQLite or live-filesystem probes, so decoding cannot mutate state or confuse
recorded paths with their current targets.
"""

from __future__ import annotations
import json
import os
from collections.abc import Mapping

import xxhash

from neocortex.deduplication.inventory.scan import (
    INVENTORY_EXCLUSION_SIGNATURE_VERSION,
    MAX_INVENTORY_EXCLUSION_RULE_CHARS,
    MAX_INVENTORY_EXCLUSION_RULES,
)
from neocortex.platform_policy import UNAVAILABLE_BIRTHTIME_NS

from neocortex.workflow.self_analysis.self_analysis import (
    LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS,
    MAX_SELF_ANALYSIS_COMMAND_ARGUMENTS,
    MAX_SELF_ANALYSIS_MANIFEST_BYTES,
    MAX_SELF_ANALYSIS_STATUS_ARGUMENTS,
    SELF_ANALYSIS_MANIFEST_SCHEMA,
    SELF_ANALYSIS_PROFILE_VERSION,
    _deep_analysis_from_argv,
)


# region [01] Typed validation primitives


class InvalidSelfAnalysisManifest(ValueError):
    """Durable manifest evidence violates its strict bounded schema."""


def manifest_mapping(
    value: object,
    *,
    label: str,
    keys: frozenset[str] | None = None,
) -> Mapping[str, object]:
    """Return one manifest object after optional exact-shape validation."""

    if not isinstance(value, dict):
        raise InvalidSelfAnalysisManifest(f"{label} must be an object")
    if keys is not None and set(value) != keys:
        raise InvalidSelfAnalysisManifest(f"{label} has an incompatible shape")
    return value


def manifest_integer(value: object, *, label: str, minimum: int = 0) -> int:
    """Return a genuine, bounded-domain integer (never a boolean)."""

    if type(value) is not int or value < minimum:
        raise InvalidSelfAnalysisManifest(f"{label} must be an integer >= {minimum}")
    return value


def manifest_birthtime_ns(value: object, *, label: str, schema: str) -> int:
    """Validate the creation-time domain supported by one manifest schema."""

    if schema == SELF_ANALYSIS_MANIFEST_SCHEMA:
        minimum = UNAVAILABLE_BIRTHTIME_NS
    elif schema in LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS:
        minimum = 0
    else:
        raise InvalidSelfAnalysisManifest("manifest birthtime schema is unsupported")
    return manifest_integer(value, label=label, minimum=minimum)


def manifest_text(value: object, *, label: str, maximum: int = 32_768) -> str:
    """Return non-empty UTF-8 text within its byte budget."""

    if not isinstance(value, str) or not value:
        raise InvalidSelfAnalysisManifest(f"{label} must be bounded non-empty text")
    if len(value.encode("utf-8")) > maximum:
        raise InvalidSelfAnalysisManifest(f"{label} must be bounded non-empty text")
    return value


def _canonical_hex(value: object, *, label: str) -> str:
    raw = manifest_text(value, label=label, maximum=32)
    try:
        parsed = int(raw, 16)
    except ValueError as exc:
        raise InvalidSelfAnalysisManifest(f"{label} is not hexadecimal") from exc
    if parsed < 0 or raw != f"{parsed:x}":
        raise InvalidSelfAnalysisManifest(f"{label} is not canonical hexadecimal")
    return raw


def _string_list(
    value: object,
    *,
    label: str,
    maximum_items: int = 128,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise InvalidSelfAnalysisManifest(f"{label} must be a bounded array")
    return [manifest_text(item, label=label) for item in value]


# endregion [01]


# region [02] Inventory policy validation


def _validate_recorded_rule(value: str, *, label: str, suffix: bool) -> str:
    if len(value) > MAX_INVENTORY_EXCLUSION_RULE_CHARS:
        raise InvalidSelfAnalysisManifest(f"invalid recorded {label} rule")
    if value in {".", ".."}:
        raise InvalidSelfAnalysisManifest(f"invalid recorded {label} rule")
    if any(character in value for character in ("/", "\\", "*", "?", "[", "]")):
        raise InvalidSelfAnalysisManifest(f"invalid recorded {label} rule")
    if suffix and (not value.startswith(".") or value == "."):
        raise InvalidSelfAnalysisManifest(f"invalid recorded {label} rule")
    return value.casefold()


def _normalize_recorded_rules(
    values: list[str],
    *,
    label: str,
    suffixes: bool = False,
) -> tuple[str, ...]:
    normalized = {_validate_recorded_rule(value, label=label, suffix=suffixes) for value in values}
    if len(normalized) > MAX_INVENTORY_EXCLUSION_RULES:
        raise InvalidSelfAnalysisManifest(f"recorded {label} rules exceed their bound")
    result = tuple(sorted(normalized))
    if list(result) != values:
        raise InvalidSelfAnalysisManifest(f"recorded {label} rules are not canonical")
    return result


def _canonical_explicit_roots(value: object) -> tuple[str, ...]:
    roots = _string_list(
        value,
        label="manifest explicit roots",
        maximum_items=MAX_INVENTORY_EXCLUSION_RULES,
    )
    canonical_by_key: dict[str, str] = {}
    for root in roots:
        if "\0" in root or not os.path.isabs(root):
            raise InvalidSelfAnalysisManifest("manifest explicit root is not absolute")
        normalized = os.path.abspath(os.path.normpath(root))
        if os.path.normcase(normalized) != os.path.normcase(root):
            raise InvalidSelfAnalysisManifest("manifest explicit root is not canonical")
        key = os.path.normcase(root)
        if key in canonical_by_key:
            raise InvalidSelfAnalysisManifest("manifest explicit roots are duplicated")
        canonical_by_key[key] = root
    root_keys = tuple(sorted(canonical_by_key))
    if [canonical_by_key[key] for key in root_keys] != roots:
        raise InvalidSelfAnalysisManifest("manifest explicit roots are not canonically ordered")
    return root_keys


def _recorded_rule_groups(
    policy: Mapping[str, object],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    directory_names = _normalize_recorded_rules(
        _string_list(
            policy.get("directory_names"),
            label="manifest directory names",
            maximum_items=MAX_INVENTORY_EXCLUSION_RULES,
        ),
        label="directory-name exclusion",
    )
    directory_prefixes = _normalize_recorded_rules(
        _string_list(
            policy.get("directory_prefixes"),
            label="manifest directory prefixes",
            maximum_items=MAX_INVENTORY_EXCLUSION_RULES,
        ),
        label="directory-prefix exclusion",
    )
    directory_fragments = _normalize_recorded_rules(
        _string_list(
            policy.get("directory_fragments"),
            label="manifest directory fragments",
            maximum_items=MAX_INVENTORY_EXCLUSION_RULES,
        ),
        label="directory-fragment exclusion",
    )
    file_names = _normalize_recorded_rules(
        _string_list(
            policy.get("file_names"),
            label="manifest file names",
            maximum_items=MAX_INVENTORY_EXCLUSION_RULES,
        ),
        label="file-name exclusion",
    )
    file_suffixes = _normalize_recorded_rules(
        _string_list(
            policy.get("file_suffixes"),
            label="manifest file suffixes",
            maximum_items=MAX_INVENTORY_EXCLUSION_RULES,
        ),
        label="file-suffix exclusion",
        suffixes=True,
    )
    return (
        directory_names,
        directory_prefixes,
        directory_fragments,
        file_names,
        file_suffixes,
    )


def _validate_recorded_policy(policy: Mapping[str, object]) -> None:
    if policy.get("profile") != SELF_ANALYSIS_PROFILE_VERSION:
        raise InvalidSelfAnalysisManifest("manifest policy profile is unsupported")
    if policy.get("signature_version") != INVENTORY_EXCLUSION_SIGNATURE_VERSION:
        raise InvalidSelfAnalysisManifest("manifest policy signature version is unsupported")
    root_keys = _canonical_explicit_roots(policy.get("explicit_roots"))
    (
        directory_names,
        directory_prefixes,
        directory_fragments,
        file_names,
        file_suffixes,
    ) = _recorded_rule_groups(policy)
    signature_payload = json.dumps(
        {
            "directory_names": directory_names,
            "directory_prefixes": directory_prefixes,
            "directory_fragments": directory_fragments,
            "explicit_root_keys": root_keys,
            "file_names": file_names,
            "file_suffixes": file_suffixes,
            "restricted_allowed_file_keys": (),
            "restricted_allowed_tree_keys": (),
            "restricted_directory_names": (),
            "restricted_file_names": (),
            "restricted_file_suffixes": (),
            "restricted_root_keys": (),
            "version": INVENTORY_EXCLUSION_SIGNATURE_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    expected_signature = (
        f"{INVENTORY_EXCLUSION_SIGNATURE_VERSION}:xxh3_128:"
        f"{xxhash.xxh3_128_hexdigest(signature_payload)}"
    )
    if policy.get("signature") != expected_signature:
        raise InvalidSelfAnalysisManifest("manifest inventory policy signature is inconsistent")


# endregion [02]


# region [03] Manifest sections


def _validate_run(manifest: Mapping[str, object], *, schema: str) -> None:
    run = manifest_mapping(
        manifest.get("run"),
        label="manifest run",
        keys=frozenset(
            {
                "run_id",
                "run_kind",
                "status",
                "corpus_access_mode",
                "root",
                "root_identity",
                "state_directory",
            }
        ),
    )
    manifest_integer(run.get("run_id"), label="manifest run_id", minimum=1)
    for name in ("run_kind", "status", "corpus_access_mode", "root", "state_directory"):
        manifest_text(run.get(name), label=f"manifest run {name}")
    identity = manifest_mapping(
        run.get("root_identity"),
        label="manifest root identity",
        keys=frozenset({"device_id_hex", "file_id_hex", "birthtime_ns"}),
    )
    _canonical_hex(identity.get("device_id_hex"), label="manifest root device")
    _canonical_hex(identity.get("file_id_hex"), label="manifest root file identity")
    manifest_birthtime_ns(
        identity.get("birthtime_ns"),
        label="manifest root birthtime",
        schema=schema,
    )


def _validate_available_journal(journal: Mapping[str, object]) -> None:
    """Validate the shared cursor fields of an available journal."""

    manifest_text(journal.get("volume"), label="manifest journal volume")
    journal_id = manifest_text(journal.get("journal_id"), label="manifest journal id")
    if not journal_id.isascii() or not journal_id.isdecimal():
        raise InvalidSelfAnalysisManifest("manifest journal id is not canonical decimal text")
    if journal_id != str(int(journal_id)):
        raise InvalidSelfAnalysisManifest("manifest journal id is not canonical decimal text")
    start_usn = manifest_integer(journal.get("start_usn"), label="manifest start USN")
    end_usn = manifest_integer(journal.get("end_usn"), label="manifest end USN")
    if end_usn < start_usn:
        raise InvalidSelfAnalysisManifest("manifest journal cursor moved backwards")


def _validate_journal(
    inventory: Mapping[str, object],
    *,
    schema: str,
) -> None:
    journal = manifest_mapping(
        inventory.get("journal"),
        label="manifest journal",
    )
    if schema in LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS:
        if set(journal) != {"volume", "journal_id", "start_usn", "end_usn"}:
            raise InvalidSelfAnalysisManifest("manifest journal has an incompatible shape")
        _validate_available_journal(journal)
        return
    status = journal.get("status")
    if status == "unavailable":
        if set(journal) != {"status"}:
            raise InvalidSelfAnalysisManifest(
                "unavailable manifest journal has an incompatible shape"
            )
        if (
            inventory.get("mode") != "full"
            or inventory.get("attempts") != 1
            or inventory.get("reconciliation_records") != 0
        ):
            raise InvalidSelfAnalysisManifest(
                "unavailable manifest journal requires one unreconciled full scan"
            )
        return
    if status != "available" or set(journal) != {
        "status",
        "volume",
        "journal_id",
        "start_usn",
        "end_usn",
    }:
        raise InvalidSelfAnalysisManifest("manifest journal has an incompatible shape")
    _validate_available_journal(journal)


def _validate_inventory(manifest: Mapping[str, object], *, schema: str) -> None:
    inventory = manifest_mapping(
        manifest.get("inventory"),
        label="manifest inventory",
        keys=frozenset(
            {
                "scan_id",
                "mode",
                "attempts",
                "reconciliation_records",
                "journal",
                "policy",
            }
        ),
    )
    manifest_integer(inventory.get("scan_id"), label="manifest scan_id", minimum=1)
    if inventory.get("mode") not in {"full", "incremental"}:
        raise InvalidSelfAnalysisManifest("manifest inventory mode is unsupported")
    manifest_integer(inventory.get("attempts"), label="manifest inventory attempts")
    manifest_integer(
        inventory.get("reconciliation_records"),
        label="manifest reconciliation records",
    )
    _validate_journal(inventory, schema=schema)
    policy = manifest_mapping(
        inventory.get("policy"),
        label="manifest inventory policy",
        keys=frozenset(
            {
                "profile",
                "signature",
                "signature_version",
                "explicit_roots",
                "directory_names",
                "directory_prefixes",
                "directory_fragments",
                "file_names",
                "file_suffixes",
            }
        ),
    )
    manifest_text(policy.get("profile"), label="manifest policy profile")
    manifest_text(policy.get("signature"), label="manifest policy signature")
    manifest_text(policy.get("signature_version"), label="manifest policy signature version")
    _validate_recorded_policy(policy)


def _validate_code(manifest: Mapping[str, object]) -> None:
    code = manifest_mapping(
        manifest.get("code"),
        label="manifest code evidence",
        keys=frozenset({"route_name", "input_source", "processing_signature", "summary"}),
    )
    if code.get("route_name") != "code":
        raise InvalidSelfAnalysisManifest("manifest code route binding is incompatible")
    if code.get("input_source") != "inventory_snapshot":
        raise InvalidSelfAnalysisManifest("manifest code route binding is incompatible")
    manifest_text(
        code.get("processing_signature"),
        label="manifest code processing signature",
        maximum=4096,
    )
    manifest_mapping(code.get("summary"), label="manifest code summary")


def _validate_safety(manifest: Mapping[str, object]) -> None:
    safety = manifest_mapping(
        manifest.get("safety"),
        label="manifest safety evidence",
        keys=frozenset({"route_candidates", "file_actions", "run_actions", "organization_events"}),
    )
    if any(type(value) is not int or value != 0 for value in safety.values()):
        raise InvalidSelfAnalysisManifest("manifest safety evidence is not exact zeroes")


def _validate_commands(manifest: Mapping[str, object]) -> None:
    commands = manifest_mapping(
        manifest.get("commands"),
        label="manifest commands",
        keys=frozenset({"analyze", "status"}),
    )
    for name, maximum_items in (
        ("analyze", MAX_SELF_ANALYSIS_COMMAND_ARGUMENTS),
        ("status", MAX_SELF_ANALYSIS_STATUS_ARGUMENTS),
    ):
        argv = _string_list(
            commands.get(name),
            label=f"manifest {name} command",
            maximum_items=maximum_items,
        )
        if not argv or argv[0] != "Neocortex":
            raise InvalidSelfAnalysisManifest(f"manifest {name} command is incompatible")


def _validate_deep_analysis(
    manifest: Mapping[str, object],
    *,
    schema: str,
) -> None:
    """Bind optional v2 deep evidence exactly to the recorded analyze argv."""

    commands = manifest_mapping(manifest.get("commands"), label="manifest commands")
    analyze = _string_list(
        commands.get("analyze"),
        label="manifest analyze command",
        maximum_items=MAX_SELF_ANALYSIS_COMMAND_ARGUMENTS,
    )
    try:
        expected = _deep_analysis_from_argv(analyze)
    except ValueError as exc:
        raise InvalidSelfAnalysisManifest("manifest deep analysis command is incompatible") from exc
    recorded = manifest.get("deep_analysis")
    if schema in LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS:
        if "deep_analysis" in manifest or expected is not None:
            raise InvalidSelfAnalysisManifest(
                "legacy manifest cannot contain deep analysis evidence"
            )
        return
    if expected is None:
        if "deep_analysis" in manifest:
            raise InvalidSelfAnalysisManifest("manifest deep analysis evidence is unexpected")
        return
    recorded_mapping = manifest_mapping(
        recorded,
        label="manifest deep analysis evidence",
    )
    recorded_canonical = json.dumps(
        recorded_mapping,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    expected_canonical = json.dumps(
        expected,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if recorded_canonical != expected_canonical:
        raise InvalidSelfAnalysisManifest("manifest deep analysis evidence is inconsistent")


# endregion [03]


# region [04] Public codec


def canonical_self_analysis_manifest(manifest: Mapping[str, object]) -> str:
    """Serialize a decoded manifest exactly as durable evidence requires."""

    return json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_bounded_json(raw: object, byte_count: object) -> Mapping[str, object]:
    if not isinstance(raw, str) or type(byte_count) is not int:
        raise InvalidSelfAnalysisManifest("self-analysis manifest has an invalid byte bound")
    if not 0 < byte_count <= MAX_SELF_ANALYSIS_MANIFEST_BYTES:
        raise InvalidSelfAnalysisManifest("self-analysis manifest has an invalid byte bound")
    if len(raw.encode("utf-8")) != byte_count:
        raise InvalidSelfAnalysisManifest("self-analysis manifest has an invalid byte bound")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidSelfAnalysisManifest("self-analysis manifest is malformed JSON") from exc
    manifest = manifest_mapping(
        decoded,
        label="self-analysis manifest",
    )
    common_keys = frozenset({"schema", "run", "inventory", "code", "safety", "commands"})
    if set(manifest) not in {common_keys, common_keys | {"deep_analysis"}}:
        raise InvalidSelfAnalysisManifest("self-analysis manifest has an incompatible shape")
    return manifest


def decode_self_analysis_manifest(raw: object, byte_count: object) -> dict[str, object]:
    """Decode and validate one canonical, bounded manifest without live I/O."""

    manifest = _decode_bounded_json(raw, byte_count)
    schema = manifest.get("schema")
    if schema not in {
        SELF_ANALYSIS_MANIFEST_SCHEMA,
        *LEGACY_SELF_ANALYSIS_MANIFEST_SCHEMAS,
    }:
        raise InvalidSelfAnalysisManifest("self-analysis manifest schema is unsupported")
    _validate_run(manifest, schema=str(schema))
    _validate_inventory(manifest, schema=str(schema))
    _validate_code(manifest)
    _validate_safety(manifest)
    _validate_commands(manifest)
    _validate_deep_analysis(manifest, schema=str(schema))
    canonical = canonical_self_analysis_manifest(manifest)
    if canonical != raw:
        raise InvalidSelfAnalysisManifest("self-analysis manifest is not canonical JSON")
    return dict(manifest)


__all__ = [
    "InvalidSelfAnalysisManifest",
    "canonical_self_analysis_manifest",
    "decode_self_analysis_manifest",
    "manifest_birthtime_ns",
    "manifest_integer",
    "manifest_mapping",
    "manifest_text",
]


# endregion [04]
