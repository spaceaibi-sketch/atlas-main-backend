"""20-per-request defaults, cron compatibility and exact-quote repair."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unicodedata

from ai.worker import process_pending_ai
from tests.test_sec_ai_batch_requests import prepared_db, statuses, BatchClient
from shared.ai_scoring import validate_financial_score

ROOT = Path(__file__).resolve().parents[1]


def test_sec_default_is_20_items_not_single_request(tmp_path):
    db = prepared_db(tmp_path, 22)
    fake = BatchClient()
    stats = process_pending_ai(db, fake, limit=0)
    assert stats['analysed'] == 22 and stats['batches'] == 2
    assert len(fake.calls) == 2
    assert [len(json.loads(c['input'])['items']) for c in fake.calls] == [20, 2]
    db.close()


def test_invalid_batch_quote_is_retried_once_only_for_affected_item(tmp_path):
    db = prepared_db(tmp_path, 3)
    def damage(payload, request):
        if 'results' in payload:
            payload['results'][1]['ai_evidence'] = 'a quote that does not appear anywhere'
        return payload
    fake = BatchClient(damage)
    stats = process_pending_ai(db, fake, limit=0, batch_size=20)
    assert stats['analysed'] == 3 and stats['errors'] == 0
    assert stats['batches'] == 1 and stats['evidence_retries'] == 1
    assert stats['evidence_recovered'] == 1 and len(fake.calls) == 2
    assert fake.calls[1]['text']['format']['name'] == 'sec_item_analysis'
    assert 'IMPORTANT RETRY' in fake.calls[1]['instructions']
    assert statuses(db) == ['analysed'] * 3
    db.close()


def test_unrepairable_quote_only_fails_single_item(tmp_path):
    db = prepared_db(tmp_path, 3)
    def damage(payload, request):
        if 'results' in payload:
            payload['results'][1]['ai_evidence'] = 'fabricated'
        elif 'agreement 2.' in request['input']:
            payload['ai_evidence'] = 'still fabricated'
        return payload
    fake = BatchClient(damage)
    stats = process_pending_ai(db, fake, limit=0, batch_size=20)
    assert stats['analysed'] == 2 and stats['errors'] == 1
    assert stats['evidence_retries'] == 1 and stats['evidence_recovered'] == 0
    assert statuses(db) == ['analysed', 'error', 'analysed']
    db.close()



def test_singleton_quote_failure_is_retried_not_terminal(tmp_path):
    db = prepared_db(tmp_path, 1)
    class FirstBad(BatchClient):
        def __init__(self):
            super().__init__()
            self.once = True
        def create(self, **kwargs):
            answer = super().create(**kwargs)
            if self.once:
                self.once = False
                value = json.loads(answer.output_text)
                value['ai_evidence'] = 'not literally in the document'
                return SimpleNamespace(status='completed', output_text=json.dumps(value))
            return answer
    fake = FirstBad()
    stats = process_pending_ai(db, fake, limit=0, batch_size=20)
    assert stats['analysed'] == 1 and stats['evidence_recovered'] == 1
    assert stats['errors'] == 0 and len(fake.calls) == 2
    assert statuses(db) == ['analysed']
    db.close()

def test_unicode_normalization_does_not_accept_a_paraphrase():
    valid = dict(ai_impact=1, ai_confidence=8, ai_reason='Test',
        ai_evidence='caf\u00e9 raised revenue', ai_monetary_effect=0,
        ai_monetary_effect_reason='Unquantified', price_change_1w_pct=0,
        price_change_reason='Neutral')
    validate_financial_score(valid, evidence_text='The cafe\u0301 raised revenue in June.')
    valid['ai_evidence'] = 'cafe raised revenue'
    import pytest
    with pytest.raises(ValueError, match='Verbatim'):
        validate_financial_score(valid, evidence_text='The cafe\u0301 raised revenue in June.')


def test_canonical_and_compatibility_launchers_share_20_default():
    import re
    for canonical, legacy, module in (
        ('run_ai_sec_scoring.sh', 'run_ai_sec_analys.sh', 'ai.scoring_cli'),
        ('run_ai_news_scoring.sh', 'run_ai_news_analys.sh', 'news_pipeline.scoring_cli')):
        src = (ROOT / canonical).read_text()
        assert '-m ' + module in src
        assert '"$@"' in src
        assert '--batch-size 20 "$@"' not in src  # defaults now come from .env
        assert canonical in (ROOT / legacy).read_text()
        for cmd in (canonical, legacy):
            proc = subprocess.run(['bash', str(ROOT / cmd), '--help'],
                cwd=ROOT, capture_output=True, text=True, timeout=15)
            assert proc.returncode == 0, (cmd, proc.stderr)
            assert '--batch-size' in proc.stdout


def _fake_crontab(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    command = bindir / 'crontab'
    command.write_text('''#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  -l) if [[ -f "$TEST_CRONTAB" ]]; then cat "$TEST_CRONTAB"; else exit 1; fi ;;
  -) cat > "$TEST_CRONTAB" ;;
  -r) rm -f "$TEST_CRONTAB" ;;
  *) exit 2 ;;
esac
''')
    command.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get('PATH',''),
               TEST_CRONTAB=str(tmp_path / 'crontab.txt'))
    return env, tmp_path / 'crontab.txt'
