"""Ensure the public Windows batch name matches its Python dispatcher."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_windows_batch_uses_supported_dispatch_name():
    bat = (ROOT / 'extract_recent_filings.bat').read_text()
    assert '-m sec_edgar.windows_launcher extract_recent_filings %*' in bat


def test_full_name_and_legacy_alias_dispatch_to_sec_metadata_help():
    for name in ('extract_recent_filings', 'extract'):
        done = subprocess.run(
            [sys.executable, '-m', 'sec_edgar.windows_launcher', name, '--help'],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        assert done.returncode == 0, (name, done.stdout, done.stderr)
        assert '--max-atom-pages' in done.stdout
        assert '--hours' in done.stdout
        assert 'usage:' in done.stdout.lower()


def test_forms_launcher_still_dispatches():
    done = subprocess.run(
        [sys.executable, '-m', 'sec_edgar.windows_launcher', 'extract_forms_items', '--help'],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert done.returncode == 0, done.stderr
    assert 'usage:' in done.stdout.lower()


def test_invalid_command_exits_with_usage_not_network():
    done = subprocess.run(
        [sys.executable, '-m', 'sec_edgar.windows_launcher', 'unsupported_command'],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert done.returncode == 2
    assert 'extract_recent_filings' in done.stderr
