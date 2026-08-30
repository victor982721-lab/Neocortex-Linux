"""Bounded read-only queries over persisted PDF derived indexes."""

from __future__ import annotations
import json
from pathlib import Path

from .pdf_state import pdf_database


# region [01] Query bounds

MAX_SEARCH_RESULTS = 1000
MAX_LAYOUT_MEMBERS_PER_GROUP = 20


# endregion [01]


# region [02] Layout families


def list_layout_groups(state_path: Path, limit: int = 20) -> list[dict]:
    """Return bounded, explainable layout families from the active generation."""

    if not 1 <= limit <= 100:
        raise ValueError("layout group limit must be between 1 and 100")
    with pdf_database(state_path, readonly=True) as connection:
        groups = connection.execute(
            """SELECT g.relation_run_id,g.group_key,g.member_count,
            g.minimum_edge_score,g.evidence_json,
            representative.path AS representative_path
            FROM layout_groups g
            LEFT JOIN documents representative
              ON representative.file_key=g.representative_file_key
            WHERE g.relation_run_id=(
                SELECT relation_run_id FROM similarity_state
                WHERE signature_kind='layout'
            )
            ORDER BY g.member_count DESC,g.minimum_edge_score DESC,g.group_key
            LIMIT ?""",
            (limit,),
        ).fetchall()
        results: list[dict] = []
        for group in groups:
            results.append(
                {
                    "group_key": group["group_key"],
                    "member_count": int(group["member_count"]),
                    "minimum_edge_score": float(group["minimum_edge_score"]),
                    "representative_path": group["representative_path"],
                    "members": [],
                    "members_truncated": False,
                    "evidence": json.loads(group["evidence_json"]),
                }
            )
        if not groups:
            return results

        bounded_member_select = """SELECT ? AS group_ordinal,d.path AS member_path
            FROM layout_group_members m JOIN documents d ON d.file_key=m.file_key
            WHERE m.relation_run_id=? AND m.group_key=?
            ORDER BY d.path COLLATE NOCASE,d.file_key LIMIT ?"""
        member_parameters: list[object] = []
        member_selects: list[str] = []
        for ordinal, group in enumerate(groups):
            member_selects.append(f"SELECT * FROM ({bounded_member_select})")
            member_parameters.extend(
                (
                    ordinal,
                    int(group["relation_run_id"]),
                    group["group_key"],
                    MAX_LAYOUT_MEMBERS_PER_GROUP,
                )
            )
        member_rows = connection.execute(
            "SELECT group_ordinal,member_path FROM ("
            + " UNION ALL ".join(member_selects)
            + ") ORDER BY group_ordinal,member_path COLLATE NOCASE",
            member_parameters,
        )
        for member in member_rows:
            results[int(member["group_ordinal"])]["members"].append(
                member["member_path"]
            )
        for result in results:
            result["members_truncated"] = result["member_count"] > len(
                result["members"]
            )
        return results


# endregion [02]


# region [03] Full-text search


def search_pdf_state(
    state_path: Path,
    query: str,
    limit: int = 20,
) -> list[dict]:
    if limit < 1:
        raise ValueError("search limit must be positive")
    if limit > MAX_SEARCH_RESULTS:
        raise ValueError(f"search limit cannot exceed {MAX_SEARCH_RESULTS}")
    with pdf_database(state_path, readonly=True) as connection:
        rows = connection.execute(
            """SELECT f.path,f.page_number,
            snippet(page_fts,3,'[',']',' … ',24) snippet,
            bm25(page_fts) rank
            FROM page_fts f JOIN documents d ON d.file_key=f.file_key
            WHERE page_fts MATCH ? AND d.status IN ('done','partial')
            ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# endregion [03]
