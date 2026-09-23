"""同案合并复核 (group review of duplicate pending queries).

Pending ``needs_review`` match logs can be handled together when they are
effectively the same case. Two logs belong to the same group only when *all*
of the following match after normalisation:

* the road (street) tokens;
* the locality tokens;
* the extracted house number;
* the ordered list of candidate address ids;
* the candidate list length (order);
* every candidate score.

A group signature binds those facts into an opaque token. Applying a group
decision (confirm the preferred candidate / override to a shared candidate /
reject all as no-match) re-checks, inside one SQLite write transaction, that
each member is still pending, carries the same signature and that the member
set itself has not grown or shrunk. Any drift aborts the transaction with
:class:`GroupConflict` and not a single row is written.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import db
from .normalization import Normalized, normalize


class GroupConflict(Exception):
    """Raised when a group decision no longer matches the live queue state."""


# ------------------------------------------------------------- group identity


def _candidate_fingerprint(candidates_json: str) -> list[dict[str, Any]]:
    """Ordered ``(address id, score)`` pairs exactly as stored on the log."""
    candidates = json.loads(candidates_json or "[]")
    return [
        {"id": int(c["address"]["id"]), "score": c["score"]}
        for c in candidates
    ]


def group_signature(*, normalized: Normalized,
                    candidates: list[dict[str, Any]]) -> str:
    """Stable opaque signature of a pending log's review identity.

    Scores are rounded to 6 decimal places before hashing so float re-parsing
    noise (``0.7`` vs ``0.7000000000000001``) cannot split identical groups.
    """
    payload = {
        "street": sorted(set(normalized.street_tokens)),
        "locality": sorted(set(normalized.locality_tokens)),
        "house": normalized.house or "",
        "candidate_ids": [c["id"] for c in candidates],
        "candidate_scores": [round(float(c["score"]), 6) for c in candidates],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def signature_for_log(row: sqlite3.Row) -> str:
    return group_signature(
        normalized=normalize(row["query"]),
        candidates=_candidate_fingerprint(row["candidates_json"]),
    )


# --------------------------------------------------------------- serialization


def _group_payload(signature: str, rows: list[sqlite3.Row]) -> dict[str, Any]:
    newest_first = sorted(rows, key=lambda r: r["id"], reverse=True)
    member_rows = sorted(rows, key=lambda r: r["id"])
    sample = newest_first[0]
    return {
        "signature": signature,
        "member_ids": [r["id"] for r in member_rows],
        "member_count": len(member_rows),
        "queries": [
            {"id": m["id"], "query": m["query"], "created_at": m["created_at"]}
            for m in member_rows
        ],
        "normalized": {
            "text": normalize(sample["query"]).text,
            "house": normalize(sample["query"]).house,
        },
        "best_id": sample["candidate_id"],
        "best_score": sample["score"],
        # Candidate lists are identical by construction; reuse the newest one.
        "candidates": json.loads(sample["candidates_json"]),
        "created_at": member_rows[0]["created_at"],
        "latest_at": newest_first[0]["created_at"],
    }


# ----------------------------------------------------------------- querying


def list_review_groups(conn: sqlite3.Connection,
                       limit: int = 500) -> list[dict[str, Any]]:
    """Group the pending queue by review identity; keep groups with 2+ rows.

    Newest groups (highest member id) come first so fresh duplicate cases sit
    at the top of the page.
    """
    rows = conn.execute(
        """
        SELECT * FROM match_logs
         WHERE status = 'needs_review'
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    buckets: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        buckets.setdefault(signature_for_log(row), []).append(row)
    groups = [
        _group_payload(sig, members)
        for sig, members in buckets.items()
        if len(members) >= 2
    ]
    groups.sort(key=lambda g: g["member_ids"][-1], reverse=True)
    return groups


# ----------------------------------------------------------- group decisions


def _score_for_override(member: sqlite3.Row, selected_id: int) -> float | None:
    for cand in json.loads(member["candidates_json"]):
        if int(cand["address"]["id"]) == selected_id:
            return cand["score"]
    return None


def apply_group_review(conn: sqlite3.Connection, *, action: str,
                       signature: str, member_ids: list[int],
                       selected_id: int | None) -> dict[str, Any]:
    """Apply one decision to a whole group atomically.

    Runs inside ``BEGIN IMMEDIATE`` so the re-validation below happens against
    the same snapshot the UPDATEs commit on. Raises :class:`GroupConflict`
    when any member has been reviewed, changed signature, or the member set
    differs; the whole transaction is then rolled back.
    """
    if action == "overridden" and selected_id is None:
        raise ValueError("改派时必须提供 selected_id")

    with db.immediate_transaction(conn):
        if action == "overridden":
            if db.get_address(conn, selected_id) is None:
                raise GroupConflict("所选标准地址不存在")

        members = db.get_match_logs(conn, member_ids)
        # 1. every requested member still exists and is still pending ...
        if len(members) != len(set(member_ids)):
            raise GroupConflict("组成员已发生变化（有记录不存在），请刷新后重试")

        for member in members:
            if member["status"] != "needs_review":
                raise GroupConflict(
                    f"记录 #{member['id']} 已被复核，整组状态已变化，请刷新后重试"
                )
            # 2. ... and no member's review identity has changed ...
            if signature_for_log(member) != signature:
                raise GroupConflict(
                    f"记录 #{member['id']} 的候选或归一化结果已变化，请刷新后重试"
                )

        # 3. ... and the group still contains exactly the same members.
        current = db.list_match_logs_by_status(conn, "needs_review")
        live_ids = {
            row["id"] for row in current
            if signature_for_log(row) == signature
        }
        if live_ids != set(member_ids):
            raise GroupConflict("组成员集合已变化（有新记录加入或被移出），请刷新后重试")

        resolved: list[dict[str, Any]] = []
        for member in members:
            if action == "confirmed":
                final_selected = member["candidate_id"]
                score = member["score"]
            elif action == "rejected":
                final_selected = None
                score = None
            else:  # overridden
                final_selected = selected_id
                score = _score_for_override(member, selected_id)
                if score is None:
                    raise GroupConflict(
                        f"地址 #{selected_id} 不是记录 #{member['id']} 的候选，"
                        "无法整组改派"
                    )
            db.review_match_log(
                conn,
                member["id"],
                action=action,
                selected_id=final_selected,
                score=score,
            )
            resolved.append({"match_id": member["id"],
                             "selected_id": final_selected, "score": score})

        return {"ok": True, "action": action, "signature": signature,
                "reviewed": resolved, "count": len(resolved)}
