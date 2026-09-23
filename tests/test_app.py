"""Tests for normalisation, scoring and the FastAPI end-to-end flow."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: E402
from app.matcher import (  # noqa: E402
    levenshtein_ratio,
    match_address,
    score_house,
)
from app.normalization import normalize  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------- normalisation


def test_unicode_and_case_unification():
    n = normalize("１２３ ＭＡＩＮ ＳＴＲＥＥＴ")
    # house number is split out of the comparable text
    assert n.house == "123"
    assert "123" not in n.text
    assert "main" in n.tokens
    # street -> st
    assert "st" in n.tokens


def test_abbreviations_and_diacritics():
    n = normalize("Müller Straße 45")
    assert "muller" in n.tokens
    assert n.house == "45"


def test_house_number_formats():
    assert normalize("12 Main St").house == "12"
    assert normalize("#12-B Main St").house == "12-b"
    assert normalize("No. 12b Main Road").house == "12b"
    assert normalize("北京东路88号").house == "88"
    # 5-digit trailing value is a postcode, not a house number
    assert normalize("Main St, Austin TX 73301").house is None
    # building refs don't grab the house number
    assert normalize("99 SW 26th Ave Unit 7B, Portland").house == "99"


def test_cjk_traditional_to_simplified_and_fullwidth():
    n = normalize("北京東路８８號 3楼")
    assert n.house == "88"
    assert "东" in n.tokens          # 東 -> 东
    assert "北京" in n.tokens        # bigram


# ------------------------------------------------------------------ scoring


def test_levenshtein_ratio():
    assert levenshtein_ratio("main", "main") == 1.0
    assert levenshtein_ratio("", "x") == 0.0
    assert levenshtein_ratio("main", "mn") < 0.7


def test_house_scoring():
    assert score_house("12", "12") == 1.0
    assert score_house("12b", "12") == 0.7
    assert score_house(None, None) == 0.5
    assert score_house("12", None) == 0.15
    assert score_house("100", "1000") < 0.5
    assert score_house("12", "99") == 0.0


# ------------------------------------------------------------- end-to-end API


def test_health_and_sample_loaded(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["address_count"] >= 15


def test_web_page_and_static(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "地址匹配" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_match_abbreviation_auto(client):
    r = client.post("/api/match", json={"query": "123 main st., New York, NY 10001"})
    data = r.json()
    assert data["status"] == "auto_matched"
    assert data["best_id"] == 1
    assert data["best_score"] >= 0.8


def test_match_house_conflict_needs_review(client):
    r = client.post("/api/match", json={"query": "1010 Lake Dr, Austin TX"})
    data = r.json()
    # street tokens match fully but the closest house number differs -> review
    best = data["candidates"][0]
    assert best["token_f1"] >= 0.99
    assert data["status"] == "needs_review"
    conflict_fields = {c["field"] for c in best["conflicts"]}
    assert "house_number" in conflict_fields


def test_match_with_unit_suffix(client):
    r = client.post("/api/match",
                    json={"query": "99 SW 26th Ave Unit 7B, Portland OR"})
    data = r.json()
    assert data["normalized"]["house"] == "99"
    assert data["best_id"] == 14
    assert data["status"] == "auto_matched"


def test_match_terrace_abbreviation(client):
    r = client.post("/api/match", json={"query": "742 Evergreen Tce"})
    data = r.json()
    assert data["best_id"] == 15
    assert data["status"] == "auto_matched"


def test_match_chinese(client):
    r = client.post("/api/match", json={"query": "北京東路８８號 3楼, 上海"})
    data = r.json()
    assert data["normalized"]["house"] == "88"
    assert data["best_id"] == 10
    assert data["status"] == "auto_matched"


def test_match_no_match(client):
    r = client.post("/api/match", json={"query": "1 Milky Way, Mars Base"})
    assert r.json()["status"] == "no_match"


def test_batch_and_review_flow(client):
    r = client.post("/api/match/batch", json={
        "queries": ["Hillcrest Road, Dallas", "123 main st ny"],
    })
    data = r.json()
    assert data["summary"]["total"] == 2
    # ambiguous query (50 vs 52 Hillcrest, no house number) needs review
    ambiguous = data["results"][0]
    assert ambiguous["status"] == "needs_review"
    log_id = ambiguous["match_id"]

    # review queue lists it
    r = client.get("/api/matches", params={"status": "needs_review"})
    ids = [item["id"] for item in r.json()["items"]]
    assert log_id in ids

    # confirm the system's preferred candidate
    r = client.post(f"/api/matches/{log_id}/review", json={"action": "confirmed"})
    assert r.status_code == 200
    r = client.get(f"/api/matches/{log_id}")
    assert r.json()["status"] == "reviewed"
    assert r.json()["review_action"] == "confirmed"


def test_override_review(client):
    r = client.post("/api/match", json={"query": "Hillcrest Rd, Dallas TX"})
    log_id = r.json()["match_id"]
    candidates = r.json()["candidates"]
    second = candidates[1]["address"]["id"]
    r = client.post(f"/api/matches/{log_id}/review",
                    json={"action": "overridden", "selected_id": second})
    assert r.status_code == 200
    detail = client.get(f"/api/matches/{log_id}").json()
    assert detail["review_action"] == "overridden"
    assert detail["selected_id"] == second


def test_csv_upload_and_dedup(client):
    csv_bytes = b"id,full_address,town,zip\nX1,9 Test Lane,Testville,99999\n"
    r = client.post("/api/addresses/upload",
                    files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    r2 = client.post("/api/addresses/upload",
                     files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates"] == 1

    r = client.post("/api/match", json={"query": "9 Test Ln, Testville 99999"})
    assert r.json()["best_id"] is not None


# ----------------------------------------------------- CSV preview / confirm


def _preview_csv(prefix: str) -> bytes:
    # line 2 valid, line 3 invalid (all blanks), line 4 valid,
    # line 5 an in-file repeat of line 2 (duplicate).
    return (
        "external_id,address,city,zip\n"
        f"{prefix}-001,1 Preview Way,Previewton,10001\n"
        ",,,\n"
        f"{prefix}-002,2 Preview Ave,Previewton,10002\n"
        f"{prefix}-001,1 Preview Way,Previewton,10001\n"
    ).encode()


def test_preview_then_confirm_writes_only_valid(client):
    csv_bytes = _preview_csv("PA")
    before = client.get("/api/health").json()["address_count"]
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] == 2
    assert data["duplicates"] == 1
    assert data["invalid"] == 1
    assert data["invalid_lines"] == [3]
    assert data["total"] == 4
    token = data["preview_token"]

    # Preview must not have written anything yet.
    assert client.get("/api/health").json()["address_count"] == before

    # Confirm requires the file to be re-submitted alongside the token.
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["inserted"] == 2
    assert result["duplicates"] == 1
    assert result["invalid"] == 1
    assert result["invalid_lines"] == [3]
    assert client.get("/api/health").json()["address_count"] == before + 2

    # The voucher is single-use: confirming again fails.
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 404


def test_preview_uses_header_aliases(client):
    csv_bytes = b"id,addr,town,postcode\nPAL,7 Alpha Road,Alphaville,20001\n"
    r = client.post("/api/addresses/preview",
                    files={"file": ("a.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] == 1
    assert data["invalid"] == 0
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("a.csv", csv_bytes, "text/csv")},
        data={"preview_token": data["preview_token"]},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 1


def test_preview_confirm_rejects_changed_file(client):
    csv_bytes = _preview_csv("PB")
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    token = r.json()["preview_token"]
    before = client.get("/api/health").json()["address_count"]

    # A different file body carrying the same token must be refused.
    tampered = csv_bytes.replace(b"2 Preview Ave", b"99 Other Street")
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", tampered, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 409
    # Nothing was written.
    assert client.get("/api/health").json()["address_count"] == before

    # The voucher is not consumed by the failed attempt, but the original file
    # was never confirmed; re-preview fresh (unique ids) data and confirm it.
    fresh = (
        "external_id,address\nPB2-001,3 Fresh Court\nPB2-002,4 Fresh Plaza\n"
    ).encode()
    r = client.post("/api/addresses/preview",
                    files={"file": ("f.csv", fresh, "text/csv")})
    assert r.status_code == 200
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("f.csv", fresh, "text/csv")},
        data={"preview_token": r.json()["preview_token"]},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2


def test_preview_confirm_rejects_changed_table(client):
    csv_bytes = _preview_csv("PC")
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    token = r.json()["preview_token"]
    before = client.get("/api/health").json()["address_count"]

    # Another writer (the legacy direct-upload endpoint) mutates the table.
    other = b"id,address\nPCINT,8 Interloper Lane\n"
    assert client.post("/api/addresses/upload",
                       files={"file": ("o.csv", other, "text/csv")}).status_code == 200

    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 409
    # Only the interloper was written; the stale preview wrote nothing.
    assert client.get("/api/health").json()["address_count"] == before + 1

    # Re-preview fresh (unique ids) data against the new snapshot, then confirm.
    fresh = (
        "external_id,address\nPC2-001,5 New Crescent\nPC2-002,6 New Gardens\n"
    ).encode()
    r = client.post("/api/addresses/preview",
                    files={"file": ("f.csv", fresh, "text/csv")})
    assert r.status_code == 200
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("f.csv", fresh, "text/csv")},
        data={"preview_token": r.json()["preview_token"]},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2


def test_preview_confirm_unknown_token(client):
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", _preview_csv("PD"), "text/csv")},
        data={"preview_token": "does-not-exist"},
    )
    assert r.status_code == 404


def test_preview_rejects_non_csv(client):
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_preview_invalid_lines_use_physical_numbers(client):
    # A blank physical line between data rows is skipped by the csv module and
    # not counted; the trailing invalid row still reports its true line number.
    content = (
        b"external_id,address\n"   # line 1 header
        b"PLN1,11 Line Lane\n"     # line 2 valid
        b"\n"                       # line 3 blank -> skipped
        b",\n"                      # line 4 invalid
    )
    r = client.post("/api/addresses/preview",
                    files={"file": ("l.csv", content, "text/csv")})
    data = r.json()
    assert data["valid"] == 1
    assert data["invalid"] == 1
    assert data["invalid_lines"] == [4]


# --------------------------------------------------------- merged 同案 review


def _upload_group_address(client, prefix: str) -> int:
    """Upload a unique low-scoring address; return its standard-address id."""
    csv_bytes = (
        f"external_id,address,city,state\n"
        f"{prefix}-STD,{prefix} Group Way,{prefix}ville,GT\n"
    ).encode()
    r = client.post("/api/addresses/upload",
                    files={"file": ("g.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    listing = client.get("/api/addresses?limit=500").json()
    return next(a["id"] for a in listing["items"] if a["external_id"] == f"{prefix}-STD")


def _match_three_variants(client, prefix: str) -> list[int]:
    """Three spellings that normalise to the exact same address.

    The standard address has no house number, so supplying one keeps every
    variant in needs_review (token F1 1.0 but house score 0.15) while sharing
    the exact same normalised road/locality/house signature.
    """
    queries = [
        f"99 {prefix} Group Way, {prefix}ville GT",
        f"99 {prefix} GROUP way, {prefix}ville GT",
        f"99 {prefix} Group Way!!!, {prefix}ville, GT",
    ]
    ids = []
    for q in queries:
        r = client.post("/api/match", json={"query": q})
        assert r.json()["status"] == "needs_review", q
        ids.append(r.json()["match_id"])
    assert len(set(ids)) == 3
    return ids


def _find_group(client, member_ids):
    data = client.get("/api/review-groups").json()
    wanted = set(member_ids)
    matches = [g for g in data["items"] if set(g["member_ids"]) == wanted]
    assert len(matches) == 1, data
    return matches[0]


def test_review_groups_endpoint_and_signature_rules(client):
    addr_id = _upload_group_address(client, "ALPHA")
    ids = _match_three_variants(client, "ALPHA")

    data = client.get("/api/review-groups").json()
    assert data["total"] >= 1
    group = _find_group(client, ids)
    assert group["member_count"] == 3
    # Original queries (and count) are exposed for the web page.
    assert [q["id"] for q in group["queries"]] == sorted(ids)
    assert all("query" in q for q in group["queries"])
    assert group["best_id"] == addr_id
    assert [c["address"]["id"] for c in group["candidates"]]

    # A different house number is a different signature -> not grouped.
    r = client.post("/api/match", json={"query": "999 ALPHA Group Way, ALPHAVILLE GT"})
    assert r.json()["status"] == "needs_review"
    group_after = _find_group(client, ids)
    assert r.json()["match_id"] not in group_after["member_ids"]

    # Different candidate score content also splits groups: the only candidate
    # here is a brand-new address of its own.
    other = _upload_group_address(client, "BRAVO")
    r = client.post("/api/match", json={"query": "99 BRAVO Group Way, BRAVOVILLE GT"})
    assert r.json()["status"] == "needs_review"
    data = client.get("/api/review-groups").json()
    singleton = [g for g in data["items"]
                 if r.json()["match_id"] in g["member_ids"]]
    assert singleton == []  # a lone record is not a group (needs 2+ members)
    assert other


def test_group_confirm_applies_to_all_members(client):
    _upload_group_address(client, "CHARLIE")
    ids = _match_three_variants(client, "CHARLIE")
    group = _find_group(client, ids)

    r = client.post("/api/review-groups/review", json={
        "action": "confirmed",
        "signature": group["signature"],
        "member_ids": group["member_ids"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert {x["match_id"] for x in body["reviewed"]} == set(ids)

    for log_id in ids:
        detail = client.get(f"/api/matches/{log_id}").json()
        assert detail["status"] == "reviewed"
        assert detail["review_action"] == "confirmed"
        assert detail["selected_id"] == group["best_id"]
        assert detail["score"] == group["best_score"]

    # The group vanishes from the pending queue.
    data = client.get("/api/review-groups").json()
    assert all(not (set(ids) & set(g["member_ids"])) for g in data["items"])


def test_group_conflict_when_member_already_reviewed_writes_nothing(client):
    _upload_group_address(client, "DELTA")
    ids = _match_three_variants(client, "DELTA")
    group = _find_group(client, ids)

    # A parallel reviewer handles one member singly first.
    r = client.post(f"/api/matches/{ids[0]}/review", json={"action": "rejected"})
    assert r.status_code == 200

    # The stale group decision must fail with 409 and write nothing else.
    r = client.post("/api/review-groups/review", json={
        "action": "confirmed",
        "signature": group["signature"],
        "member_ids": group["member_ids"],
    })
    assert r.status_code == 409
    for log_id in ids[1:]:
        detail = client.get(f"/api/matches/{log_id}").json()
        assert detail["status"] == "needs_review"
        assert detail["review_action"] is None

    # The live group endpoint now reports the remaining two members only.
    fresh = client.get("/api/review-groups").json()
    survivor_groups = [g for g in fresh["items"] if ids[1] in g["member_ids"]]
    assert len(survivor_groups) == 1
    assert survivor_groups[0]["member_ids"] == ids[1:]


def test_group_conflict_when_member_set_grows(client):
    _upload_group_address(client, "ECHO")
    ids = _match_three_variants(client, "ECHO")
    group = _find_group(client, ids)

    # A new duplicate arrives before the decision is posted: same signature,
    # one more member.
    r = client.post("/api/match", json={"query": "99 ECHO Group Way, ECHOVILLE, GT"})
    new_id = r.json()["match_id"]
    assert r.json()["status"] == "needs_review"

    r = client.post("/api/review-groups/review", json={
        "action": "rejected",
        "signature": group["signature"],
        "member_ids": group["member_ids"],
    })
    assert r.status_code == 409
    # Nothing was written, including the newcomer.
    for log_id in ids + [new_id]:
        assert client.get(f"/api/matches/{log_id}").json()["status"] == "needs_review"

    # Re-fetch and act on the full current set.
    grown = _find_group(client, ids + [new_id])
    assert grown["member_count"] == 4
    r = client.post("/api/review-groups/review", json={
        "action": "rejected",
        "signature": grown["signature"],
        "member_ids": grown["member_ids"],
    })
    assert r.status_code == 200
    assert r.json()["count"] == 4
    for log_id in ids + [new_id]:
        detail = client.get(f"/api/matches/{log_id}").json()
        assert detail["status"] == "reviewed"
        assert detail["review_action"] == "rejected"
        assert detail["selected_id"] is None


def test_group_override_uses_each_members_candidate_score(client):
    _upload_group_address(client, "FOXTROT")
    ids = _match_three_variants(client, "FOXTROT")
    group = _find_group(client, ids)
    # A near-twin address adds a shared candidate (token F1 ~0.8, house match)
    # so an override target exists and the recorded score is the candidate-
    # specific one, not the preferred candidate's score.
    csv_bytes = b"external_id,address,city,state\nFOXTROT-DEC,FOXTROT Group Way North,FOXTROTVILLE,GT\n"
    assert client.post("/api/addresses/upload",
                       files={"file": ("d.csv", csv_bytes, "text/csv")}).status_code == 200

    # New logs against the two-candidate table with identical normalised text.
    pair = []
    for q in ("99 FOXTROT Group Way, FOXTROTVILLE, GT!!", "99 FOXTROT group way, FOXTROTVILLE, gt"):
        r = client.post("/api/match", json={"query": q})
        pair.append(r.json()["match_id"])
    two_cand = client.get(f"/api/matches/{pair[0]}").json()
    decoy_id = next(
        c["address"]["id"] for c in two_cand["candidates"]
        if c["address"]["external_id"] == "FOXTROT-DEC"
    )
    score_for_decoy = next(
        c["score"] for c in two_cand["candidates"]
        if c["address"]["id"] == decoy_id
    )
    # The override target is a real but non-preferred candidate.
    assert two_cand["candidate_id"] != decoy_id
    assert score_for_decoy != two_cand["score"]
    g = _find_group(client, pair)

    r = client.post("/api/review-groups/review", json={
        "action": "overridden",
        "signature": g["signature"],
        "member_ids": g["member_ids"],
        "selected_id": decoy_id,
    })
    assert r.status_code == 200
    for log_id in pair:
        detail = client.get(f"/api/matches/{log_id}").json()
        assert detail["review_action"] == "overridden"
        assert detail["selected_id"] == decoy_id
        assert detail["score"] == score_for_decoy

    # Overriding to an id outside the candidate list is a 409 conflict and
    # writes nothing.
    _upload_group_address(client, "GOLF")
    leftover = _find_group(client, ids)
    stranger = client.get("/api/addresses?limit=500").json()
    stranger_id = next(a["id"] for a in stranger["items"]
                       if a["external_id"] == "GOLF-STD")
    r = client.post("/api/review-groups/review", json={
        "action": "overridden",
        "signature": leftover["signature"],
        "member_ids": leftover["member_ids"],
        "selected_id": stranger_id,
    })
    assert r.status_code == 409
    for log_id in ids:
        assert client.get(f"/api/matches/{log_id}").json()["status"] == "needs_review"


def test_group_review_validates_payload(client):
    _upload_group_address(client, "HOTEL")
    ids = _match_three_variants(client, "HOTEL")
    group = _find_group(client, ids)

    # Override without selected_id -> 400
    r = client.post("/api/review-groups/review", json={
        "action": "overridden",
        "signature": group["signature"],
        "member_ids": group["member_ids"],
    })
    assert r.status_code == 400

    # Unknown signature -> 409 (the member set under that signature is empty)
    r = client.post("/api/review-groups/review", json={
        "action": "rejected",
        "signature": "deadbeef",
        "member_ids": group["member_ids"],
    })
    assert r.status_code == 409
