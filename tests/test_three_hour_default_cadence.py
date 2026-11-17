"""News ingestion, news scoring and Massive default to 3-hour intervals."""

from shared import config_cron


def test_three_hour_defaults_and_explicit_overrides(monkeypatch, capsys):
    for field in config_cron.FIELDS:
        monkeypatch.delenv(field, raising=False)
    config_cron.main()
    assert capsys.readouterr().out.splitlines() == ['1', '1', '3', '1', '3', '3']
    monkeypatch.setenv('ATLAS_CRON_NEWS_HOURS', '6')
    monkeypatch.setenv('ATLAS_CRON_MASSIVE_PRICES_HOURS', '2')
    config_cron.main()
    assert capsys.readouterr().out.splitlines() == ['1', '1', '6', '1', '3', '2']
