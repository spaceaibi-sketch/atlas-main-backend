"""SEC Item in-flight form-status regression tests (no OpenAI calls)."""
import json
from types import SimpleNamespace

from ai.worker import process_pending_ai
from sec_edgar.database import connect

STAMP = "2026-10-20T12:00:00Z"


def setup_db(tmp_path):
    db = connect(tmp_path / "sec_ai_stale_test.sqlite3")
    with db:
        cid = db.execute(
            "INSERT INTO companies(cik,name,created_at,updated_at) VALUES (?,?,?,?)",
            ("0000009999", "Test Company", STAMP, STAMP),
        ).lastrowid
        fid = db.execute(
            """INSERT INTO forms(company_id,form_type,form_name,filed_date,index_url,status,created_at,updated_at)
               VALUES (?,?,?,?,?,'parsed',?,?)""",
            (cid, "8-K", "Current filing", STAMP, "https://example.org/filing", STAMP, STAMP),
        ).lastrowid
        db.execute(
            """INSERT INTO items(form_id,item_type,item_occurrence,item_name,item_text,status,created_at,updated_at)
               VALUES (?,'1.01',1,'Agreement','Test Company signed a contract.','processed',?,?)""",
            (fid, STAMP, STAMP),
        )
    return db


def valid_response():
    return SimpleNamespace(status="completed", output_text=json.dumps({
        "ai_impact": 3,
        "ai_confidence": 7,
        "ai_reason": "The contract may improve operations.",
        "ai_evidence": "Test Company signed a contract.",
        "ai_monetary_effect": 0,
        "ai_monetary_effect_reason": "0 is a placeholder: the filing does not quantify net USD effects.",
        "price_change_1w_pct": 0,
        "price_change_reason": "Neutral speculative scenario, not an observed return.",
    }))


def test_completed_response_is_discarded_if_filing_no_longer_parsed(tmp_path):
    db = setup_db(tmp_path)
    class Client:
        responses = None
        def __init__(self): self.responses = self
        def create(self, **kwargs):
            with db: db.execute("UPDATE forms SET status='extracted'")
            return valid_response()
    stats = process_pending_ai(db, Client(), limit=0)
    assert stats["analysed"] == 0
    assert stats["skipped_stale"] == 1
    assert db.execute("SELECT status FROM items").fetchone()[0] == "processed"
    db.close()


def test_failed_response_does_not_mark_item_error_if_filing_no_longer_parsed(tmp_path):
    db = setup_db(tmp_path)
    class Client:
        responses = None
        def __init__(self): self.responses = self
        def create(self, **kwargs):
            with db: db.execute("UPDATE forms SET status='error'")
            raise ValueError("Malformatted AI response")
    stats = process_pending_ai(db, Client(), limit=0)
    assert stats["errors"] == 1
    assert stats["skipped_stale"] == 1
    assert db.execute("SELECT status FROM items").fetchone()[0] == "processed"
    db.close()


def test_normal_completed_response_still_commits(tmp_path):
    db = setup_db(tmp_path)
    class Client:
        responses = None
        def __init__(self): self.responses = self
        def create(self, **kwargs): return valid_response()
    stats = process_pending_ai(db, Client(), limit=0)
    assert stats["analysed"] == 1
    assert stats["errors"] == 0
    assert db.execute("SELECT status FROM items").fetchone()[0] == "analysed"
    db.close()
