from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.code_schema import initialize_code_state
from _04_Nucleo_Operativo.code_state_interaction_analysis import (
    CODE_STATE_INTERACTION_EXAMPLE_LIMIT,
    analyze_code_state_interactions,
    parse_code_state_interaction_payload,
    state_interaction_questions,
)
from _04_Nucleo_Operativo.semantic_models import fingerprint_text


def _published_code_state(tmp_path: Path, sources: dict[str, str]) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    database = state / "code.sqlite3"
    initialize_code_state(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """INSERT INTO analysis_runs(
            analysis_run_id,framework_run_id,scan_id,processing_signature,status,
            started_ns,completed_ns,candidates,processed,cache_hits,errors)
            VALUES(1,1,1,'fixture-signature','completed',1,2,?,?,0,0)""",
            (len(sources), len(sources)),
        )
        for index, (relative, text) in enumerate(sorted(sources.items()), start=1):
            raw = text.encode()
            digest = fingerprint_text(text)
            path = str((Path("/workspace/Repository") / relative).as_posix())
            connection.execute(
                """INSERT INTO files(
                file_id,volume_id,physical_file_id,current_path,first_seen_run_id,
                last_seen_run_id,status) VALUES(?, 'fixture-volume', ?, ?,1,1,'current')""",
                (index, f"file-{index}", path),
            )
            connection.execute(
                """INSERT INTO file_versions(
                version_id,file_id,path_observed,size,mtime_ns,birthtime_ns,
                raw_xxh3_128,raw_xxh3_64_guard,text_xxh3_128,text_xxh3_64_guard,
                encoding,language,artifact_kind,generated,vendored,classification_confidence,
                classification_evidence_json,analysis_status,processing_signature,
                analyzer_id,analyzer_version,parser_kind,text_zlib,text_chars,text_truncated,
                provenance_json,first_observed_run_id,last_observed_run_id,valid_from_ns)
                VALUES(?,?,?,?,1,1,?,?,?,?, 'utf-8','python','source',0,0,1.0,'{}','complete',
                'fixture-signature','fixture','v1','ast',?,?,0,'{}',1,1,1)""",
                (
                    index,
                    index,
                    path,
                    len(raw),
                    digest.xxh3_128,
                    digest.xxh3_64_guard,
                    digest.xxh3_128,
                    digest.xxh3_64_guard,
                    zlib.compress(raw),
                    len(text),
                ),
            )
            connection.execute(
                "UPDATE files SET current_version_id=? WHERE file_id=?", (index, index)
            )
            tree = __import__("ast").parse(text)
            module = relative[:-3].replace("/", ".")
            connection.execute(
                """INSERT INTO symbols(
                symbol_id,version_id,kind,name,qualified_name,confirmed,start_line,start_column,
                end_line,end_column,start_byte,end_byte,metadata_json)
                VALUES(?,?,?,?,?,1,1,0,1,0,0,0,'{}')""",
                (index * 1000, index, "module", module.rsplit(".", 1)[-1], module),
            )
            symbol_id = index * 1000 + 1
            for node in tree.body:
                if isinstance(node, (__import__("ast").FunctionDef, __import__("ast").ClassDef)):
                    name = node.name
                    qualified = f"{module.rsplit('.', 1)[-1]}.{name}"
                    connection.execute(
                        """INSERT INTO symbols(
                        symbol_id,version_id,parent_symbol_id,kind,name,qualified_name,confirmed,
                        start_line,start_column,end_line,end_column,start_byte,end_byte,metadata_json)
                        VALUES(?,?,?,?,?,?,1,?,0,?,0,0,0,'{}')""",
                        (
                            symbol_id,
                            index,
                            index * 1000,
                            "function" if node.__class__.__name__ == "FunctionDef" else "class",
                            name,
                            qualified,
                            node.lineno,
                            node.end_lineno or node.lineno,
                        ),
                    )
                    symbol_id += 1
        connection.commit()
    finally:
        connection.close()
    return state


def test_literal_sql_is_parsed_and_dynamic_sql_remains_missing_evidence(tmp_path: Path) -> None:
    state = _published_code_state(
        tmp_path,
        {
            "_04_Nucleo_Operativo/text_fixture.py": """
def publish(connection, dynamic_sql):
    connection.execute("SELECT value FROM source_table")
    connection.execute("INSERT INTO target_table(value) SELECT value FROM source_table")
    connection.execute(dynamic_sql)
    connection.commit()
""",
        },
    )

    result = analyze_code_state_interactions(state)

    assert result.status == "partial"
    assert result.literal_sql_sites == result.parsed_sql_sites == 2
    assert result.dynamic_sql_sites == 1
    assert result.parse_error_sites == 0
    assert result.interactions_count == 2
    assert result.transaction_events_count == 1
    first, second = result.interactions
    assert first.read_tables == ("source_table",)
    assert second.read_tables == ("source_table",)
    assert second.write_tables == ("target_table",)
    assert second.logical_owner_ids == ("text",)
    assert second.state_owner_ids == ("text",)
    assert second.state_store_ids == ("sqlite:text.sqlite3",)
    assert result.transaction_events[0].event_kind == "commit"
    assert result.authority == "advisory"
    assert result.mutation_authority is False


def test_renames_paths_and_commit_spellings_do_not_invent_sql_or_store_ownership(
    tmp_path: Path,
) -> None:
    variants = {}
    for index, (path, function, call) in enumerate(
        (
            ("service.py", "compute", "commit"),
            ("result_repository.py", "build", "rollback"),
            ("state_store.py", "read", "commit"),
        )
    ):
        state = _published_code_state(
            tmp_path / str(index),
            {path: f"def {function}(connection):\n    connection.{call}()\n"},
        )
        variants[path] = analyze_code_state_interactions(state)

    assert {item.interactions_count for item in variants.values()} == {0}
    assert {item.literal_sql_sites for item in variants.values()} == {0}
    assert {item.transaction_events_count for item in variants.values()} == {1}
    assert all(item.transaction_events[0].logical_owner_ids == () for item in variants.values())
    assert all(item.transaction_events[0].state_store_ids == () for item in variants.values())


def test_execute_insert_literal_is_not_hidden_by_the_call_spelling(tmp_path: Path) -> None:
    state = _published_code_state(
        tmp_path,
        {"unknown.py": "def run(c):\n    c.execute('INSERT INTO facts(id) VALUES (1)')\n"},
    )

    result = analyze_code_state_interactions(state)

    assert result.parsed_sql_sites == 1
    assert result.interactions[0].write_tables == ("facts",)
    assert result.interactions[0].logical_owner_ids == ()
    assert result.interactions[0].state_store_ids == ()


def test_question_projection_remains_experiment_required_and_never_recommends_change(
    tmp_path: Path,
) -> None:
    state = _published_code_state(
        tmp_path,
        {
            "_04_Nucleo_Operativo/text_fixture.py": "def read(c):\n    c.execute('SELECT * FROM x')\n"
        },
    )
    analysis = analyze_code_state_interactions(state)

    specs, evaluations = state_interaction_questions(
        analysis,
        snapshot_id="fixture-snapshot",
        snapshot_freshness="current",
        rank_offset=8,
    )

    assert tuple(item.rank for item in evaluations) == (9, 10)
    assert tuple(item.question_id for item in specs) == (
        "state.static_sql_interactions_are_resolved",
        "state.declared_workflow_sql_matches_implementation",
    )
    assert all(item.inference_status == "abstained" for item in evaluations)
    assert all(item.decision is None for item in evaluations)
    assert all(item.decision_readiness == "experiment_required" for item in evaluations)
    assert all(item.authority == "advisory" for item in evaluations)
    assert all(item.mutation_authority is False for item in evaluations)


def test_wire_round_trip_rejects_forged_counts_and_decision_authority(tmp_path: Path) -> None:
    state = _published_code_state(
        tmp_path,
        {
            "_04_Nucleo_Operativo/text_fixture.py": "def read(c):\n    c.execute('SELECT * FROM x')\n"
        },
    )
    analysis = analyze_code_state_interactions(state)
    payload = json.loads(json.dumps(analysis.as_payload()))

    assert parse_code_state_interaction_payload(payload) == analysis

    forged = json.loads(json.dumps(payload))
    forged["interactions_count"] += 1
    with pytest.raises(ValueError, match="examples disagree"):
        parse_code_state_interaction_payload(forged)

    with pytest.raises(ValueError, match="advisory"):
        replace(analysis, authority="decision")


def test_bounded_examples_keep_exact_counts(tmp_path: Path) -> None:
    calls = "\n".join(
        f"    c.execute('SELECT * FROM table_{index}')"
        for index in range(CODE_STATE_INTERACTION_EXAMPLE_LIMIT + 5)
    )
    state = _published_code_state(
        tmp_path,
        {"unknown.py": f"def run(c):\n{calls}\n"},
    )

    result = analyze_code_state_interactions(state)

    assert result.interactions_count == CODE_STATE_INTERACTION_EXAMPLE_LIMIT + 5
    assert len(result.interactions) == CODE_STATE_INTERACTION_EXAMPLE_LIMIT
    assert result.interactions_truncated is True


def test_missing_state_abstains_without_nominal_evidence(tmp_path: Path) -> None:
    result = analyze_code_state_interactions(tmp_path / "absent")

    assert result.status == "abstained"
    assert result.reason == "code_state_missing"
    assert result.interactions == ()
    assert result.workflow_boundaries == ()


def test_public_envelope_rejects_ready_without_a_bound_source_publication(
    tmp_path: Path,
) -> None:
    abstained = analyze_code_state_interactions(tmp_path / "absent")
    with pytest.raises(ValueError, match="invalid readiness"):
        replace(
            abstained,
            status="ready",
            reason=None,
            analysis_run_id=1,
            source_schema_version=5,
            source_files=1,
            source_files_without_text=1,
        )
