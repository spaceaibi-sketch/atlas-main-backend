"""Regression checks for .env-controlled runtime and JSON raw archive writes."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.client import key_present, load_key, openai_client
from sec_edgar.raw_archive import archive_bytes
from shared.config import integer, number


def test_env_is_single_source_for_openai_key(tmp_path, monkeypatch):
    import sec_edgar.windows_launcher as wl
    monkeypatch.setattr(wl, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('OPENAI_API_KEY=sk-from-example-env\nOPENAI_TIMEOUT_SECONDS=300\n')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_TIMEOUT_SECONDS', raising=False)
    assert key_present()
    assert load_key() == 'sk-from-example-env'
    assert number('OPENAI_TIMEOUT_SECONDS', 60) == 300
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    (tmp_path / '.env').write_text('OPENAI_TIMEOUT_SECONDS=300\n')
    assert not key_present(), 'Do not silently fall back to old assets/key.txt'
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        load_key()


def test_runtime_env_overrides_configuration_file(tmp_path, monkeypatch):
    import sec_edgar.windows_launcher as wl
    monkeypatch.setattr(wl, 'ROOT', tmp_path)
    (tmp_path / '.env').write_text('NEWS_SCORING_BATCH_SIZE=20\nSEC_AI_BATCH_SIZE=20\n')
    monkeypatch.setenv('SEC_AI_BATCH_SIZE', '5')
    monkeypatch.delenv('NEWS_SCORING_BATCH_SIZE', raising=False)
    assert integer('SEC_AI_BATCH_SIZE', 20, minimum=1, maximum=25) == 5
    assert integer('NEWS_SCORING_BATCH_SIZE', 20, minimum=2, maximum=100) == 20
    monkeypatch.setenv('SEC_AI_BATCH_SIZE', '99')
    with pytest.raises(ValueError, match='out of range'):
        integer('SEC_AI_BATCH_SIZE', 20, minimum=1, maximum=25)


def test_json_raw_archive_and_metadata_have_distinct_paths(tmp_path):
    db_path = tmp_path / 'edgar.sqlite3'
    body = b'{"fields":["cik"],"data":[]}'
    target = archive_bytes(db_path, service='sec_company_directory',
        subject='ticker_catalog', kind='json',
        url='https://www.sec.gov/files/company_tickers_exchange.json',
        payload=body, extension='.json')
    assert target.read_bytes() == body
    manifest = target.with_suffix('.meta.json')
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data['local_file'] == target.name and data['bytes'] == len(body)
    assert data['sha256']


def test_old_non_json_raw_archive_metadata_naming_stays_compatible(tmp_path):
    target = archive_bytes(tmp_path / 'edgar.sqlite3',service='sec_filings',
        subject='filing',kind='primary',url='https://www.sec.gov/report.htm',
        payload=b'<h1>Report</h1>',extension='.htm')
    assert target.with_suffix('.json').is_file()


def test_env_cron_hours(monkeypatch, capsys):
    from shared.config_cron import FIELDS, main
    for index, key in enumerate(FIELDS):
        monkeypatch.setenv(key, str(index))
    main()
    assert capsys.readouterr().out.strip().splitlines() == [str(i) for i in range(6)]


def test_launchers_rely_on_python_env_defaults():
    root = Path(__file__).resolve().parents[1]
    for filename in ('run_ai_news_scoring.sh','run_ai_sec_scoring.sh'):
        source = (root / filename).read_text()
        assert '--batch-size 20' not in source
        assert '"$@"' in source
