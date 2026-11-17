"""Regressions: one SEC Item/news relation must not hold or roll back its peers."""
import json
from types import SimpleNamespace

from ai.worker import process_pending_ai
from news_pipeline.scoring import score_pending
from tests.test_sec_ai_batch_requests import BatchClient, prepared_db, statuses
from tests.test_ai_analysis_status_audit import NewsFake, db_with_news


def test_sec_valid_results_committed_before_slow_quote_retry(tmp_path):
    db = prepared_db(tmp_path, 3)

    def bad_one(payload, request):
        if 'results' in payload:
            payload['results'][1]['ai_evidence'] = 'hallucinated words'
        return payload

    class ObserveRetry(BatchClient):
        def create(self, **request):
            if 'IMPORTANT RETRY' in request['instructions']:
                # At the instant the retry is requested, ALL other results
                # from the first API call must already be durable.
                assert statuses(db) == ['analysed', 'processed', 'analysed']
            return super().create(**request)

    result = process_pending_ai(db, ObserveRetry(bad_one), limit=0, batch_size=3)
    assert result['analysed'] == 3 and result['evidence_retries'] == 1
    assert statuses(db) == ['analysed'] * 3
    db.close()


def test_sec_broken_batch_commits_each_success_before_next_request(tmp_path):
    db = prepared_db(tmp_path, 3)

    class BreakBatch(BatchClient):
        def create(self, **request):
            if request['text']['format']['name'] == 'sec_item_batch_analysis':
                return SimpleNamespace(status='completed', output_text='not JSON')
            if len(self.calls) == 1:
                assert statuses(db) == ['analysed', 'processed', 'processed']
            return super().create(**request)

    result = process_pending_ai(db, BreakBatch(), limit=0, batch_size=3)
    assert result['batch_fallbacks'] == 1 and result['analysed'] == 3
    db.close()


def test_sec_one_database_write_failure_cannot_rollback_others(tmp_path):
    db = prepared_db(tmp_path, 3)
    with db:
        db.execute('''CREATE TRIGGER fail_one_item BEFORE UPDATE ON items
            WHEN NEW.status='analysed' AND OLD.item_occurrence=2
            BEGIN SELECT RAISE(ABORT, 'isolated SEC database error'); END''')
    stats = process_pending_ai(db, BatchClient(), limit=0, batch_size=3)
    assert stats['analysed'] == 2 and stats['errors'] == 1
    assert statuses(db) == ['analysed', 'processed', 'analysed']
    db.close()


def test_sec_one_request_timeout_does_not_stop_next_batch(tmp_path):
    db = prepared_db(tmp_path, 4)

    class FailFirstBatch(BatchClient):
        def create(self, **request):
            if not self.calls:
                self.calls.append(request)
                raise TimeoutError('first SEC batch timed out')
            return super().create(**request)

    result = process_pending_ai(db, FailFirstBatch(), limit=0, batch_size=2)
    assert result['analysed'] == 2 and result['errors'] == 1
    assert statuses(db) == ['processed', 'processed', 'analysed', 'analysed']
    db.close()


def test_news_unparseable_batch_saves_one_relation_before_next_retry(tmp_path):
    db, _, _ = db_with_news(tmp_path, (1, 1, 1))

    class BreakBatch(NewsFake):
        def create(self, **request):
            pair_ids = [p['relation_id'] for p in json.loads(request['input'])['news_entity_pairs']]
            if len(pair_ids) > 1:
                self.calls.append(request)
                return SimpleNamespace(status='completed', output_text='bad JSON')
            if pair_ids == ['r10']:
                assert db.execute("SELECT status FROM news_company_relations WHERE relation_id='r00'").fetchone()[0] == 'analysed'
            return super().create(**request)

    stats = score_pending(db, BreakBatch(), limit=0, batch_size=20)
    assert stats['analysed_relations'] == 3 and stats['remaining'] == 0
    assert stats['batch_fallbacks'] == 1
    db.close()


def test_news_one_database_write_failure_cannot_rollback_others(tmp_path):
    db, _, _ = db_with_news(tmp_path, (1,1,1))
    with db:
        db.execute('''CREATE TRIGGER fail_one_news BEFORE UPDATE ON news_company_relations
            WHEN NEW.status='analysed' AND OLD.relation_id='r10'
            BEGIN SELECT RAISE(ABORT, 'isolated news database error'); END''')
    stats = score_pending(db, NewsFake(), limit=0)
    assert stats['analysed_relations'] == 2 and stats['errors'] == 1
    assert {r['relation_id']: r['status'] for r in db.execute(
        'SELECT relation_id,status FROM news_company_relations')} == {
        'r00': 'analysed', 'r10': 'connected', 'r20': 'analysed'}
    assert db.execute("SELECT status FROM news WHERE news_id='n2'").fetchone()[0] == 'analysed'
    db.close()


def test_news_single_bad_relation_during_fallback_does_not_block_others(tmp_path):
    db, _, _ = db_with_news(tmp_path, (1,1,1))

    class Broken(NewsFake):
        def create(self, **request):
            pairs = json.loads(request['input'])['news_entity_pairs']
            if len(pairs) > 1 or pairs[0]['relation_id'] == 'r10':
                self.calls.append(request)
                return SimpleNamespace(status='completed', output_text='bad JSON')
            return super().create(**request)

    stats = score_pending(db, Broken(), limit=0)
    assert stats['analysed_relations'] == 2
    assert stats['failed_relations'] == 1 and stats['remaining'] == 1
    assert {r['relation_id']: r['status'] for r in db.execute(
        'SELECT relation_id,status FROM news_company_relations')} == {
        'r00': 'analysed', 'r10': 'connected', 'r20': 'analysed'}
    db.close()


def test_sec_quote_retry_waits_until_all_primary_batches_are_saved(tmp_path):
    db = prepared_db(tmp_path, 4)

    class InspectOrder(BatchClient):
        def __init__(self):
            self.primary_batches = 0
            super().__init__()

        def create(self, **request):
            if request['text']['format']['name'] == 'sec_item_batch_analysis':
                self.primary_batches += 1
                if self.primary_batches == 2:
                    # First batch's valid Item is already stored, and the
                    # quote repair has not delayed this second primary batch.
                    assert statuses(db) == ['analysed', 'processed', 'processed', 'processed']
            if 'IMPORTANT RETRY' in request['instructions']:
                assert self.primary_batches == 2
                assert statuses(db) == ['analysed', 'processed', 'analysed', 'analysed']
            answer = super().create(**request)
            if self.primary_batches == 1 and request['text']['format']['name'] == 'sec_item_batch_analysis':
                value = json.loads(answer.output_text)
                value['results'][1]['ai_evidence'] = 'fabricated quote'
                return SimpleNamespace(status='completed', output_text=json.dumps(value))
            return answer

    stats = process_pending_ai(db, InspectOrder(), limit=0, batch_size=2)
    assert stats['analysed'] == 4 and stats['evidence_recovered'] == 1
    assert statuses(db) == ['analysed'] * 4
    db.close()
