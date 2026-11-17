"""Regression tests for hour-aligned cron cadence and failed-run retries."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


CRON_SH = Path(__file__).resolve().parents[1] / 'cron_job.sh'
FIRST_HOUR = int(dt.datetime(2026, 10, 26, 21, 50, tzinfo=dt.timezone.utc).timestamp())


class TestMassiveHourlyCron(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        backend = self.root / 'main_back_end'
        backend.mkdir()
        shutil.copyfile(CRON_SH, backend / 'cron_job.sh')
        (backend / 'extract_massive_prices.sh').write_text(
            '#!/usr/bin/env bash\n'
            'echo call >> "$CALLS"\n'
            '[[ "${FAIL_JOB:-0}" != 1 ]]\n'
        )
        mock_bin = self.root / 'mock-bin'
        mock_bin.mkdir()
        fake_date = mock_bin / 'date'
        fake_date.write_text('#!/usr/bin/env bash\n'
                             'if [[ "${1:-}" == +%s ]]; then printf "%s\\n" "$MOCK_NOW"; '
                             'else /bin/date "$@"; fi\n')
        fake_date.chmod(0o755)
        self.calls = self.root / 'calls'
        self.env = dict(os.environ, CALLS=str(self.calls),
                        PATH=f'{mock_bin}:{os.environ.get("PATH", "")}')
        self.backend = backend

    def run_job(self, moment: int, hours: int, fail: bool = False):
        env = dict(self.env, MOCK_NOW=str(moment), FAIL_JOB='1' if fail else '0')
        return subprocess.run(['bash', str(self.backend / 'cron_job.sh'),
                               'extract_massive_prices', str(hours)],
                              env=env, capture_output=True, text=True)

    def count(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_hourly_next_cron_tick_not_skipped_after_success(self):
        self.assertEqual(self.run_job(FIRST_HOUR, 1).returncode, 0)
        self.assertEqual(self.run_job(FIRST_HOUR + 10, 1).returncode, 0)
        self.assertEqual(self.count(), 1, 'same-hour duplicate should be skipped')
        self.assertEqual(self.run_job(FIRST_HOUR + 3600, 1).returncode, 0)
        self.assertEqual(self.count(), 2, 'next clock hour should run')

    def test_two_hour_schedule_uses_clock_hour_slots(self):
        self.assertEqual(self.run_job(FIRST_HOUR, 2).returncode, 0)
        self.assertEqual(self.run_job(FIRST_HOUR + 3600, 2).returncode, 0)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.run_job(FIRST_HOUR + 7200, 2).returncode, 0)
        self.assertEqual(self.count(), 2)

    def test_three_hour_schedule_uses_clock_hour_slots(self):
        self.assertEqual(self.run_job(FIRST_HOUR, 3).returncode, 0)
        self.assertEqual(self.run_job(FIRST_HOUR + 3600, 3).returncode, 0)
        self.assertEqual(self.run_job(FIRST_HOUR + 7200, 3).returncode, 0)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.run_job(FIRST_HOUR + 10800, 3).returncode, 0)
        self.assertEqual(self.count(), 2)

    def test_failed_runs_do_not_advance_gate(self):
        self.assertNotEqual(self.run_job(FIRST_HOUR, 1, fail=True).returncode, 0)
        self.assertEqual(self.count(), 1)
        self.assertFalse((self.root / 'data/cron_state/extract_massive_prices.last_success').exists())
        self.assertEqual(self.run_job(FIRST_HOUR + 60, 1).returncode, 0)
        self.assertEqual(self.count(), 2)


if __name__ == '__main__':
    unittest.main()
