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
