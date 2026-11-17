"""Crash-recoverable Massive price work spool and single DB-writer helpers.

A planned job is fsynced before downloading; a fetched response is atomically
fsynced before the database writer sees it. Completed jobs are deleted ONLY
after SQLite commits and verification succeeds. Replaying a committed job is
safe: price_bars has a unique (company_id, bar_time_utc) key and inserts do
not overwrite existing prices. This is at-least-once delivery, not a promise
against permanent DB/disk failures or unavailable source data.
"""
from __future__ import annotations

import json
import hashlib
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4, uuid5, NAMESPACE_URL


class SpoolError(RuntimeError):
    pass


class MassivePriceSpool:
    """Disk-backed bounded-in-memory work queue for one canonical SQLite DB."""

    def __init__(self, db_path: Path):
        path = Path(db_path).resolve()
        self.path = path.parent / (path.name + '.massive_price_spool')
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._mutex = Lock()

    def _fsync_directory(self):
        if os.name != 'posix':
            return
        fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _path(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise SpoolError('Invalid price spool job ID')
        return self.path / (job_id + '.json')

    def _atomic_write(self, job):
        path = self._path(job['id'])
        tmp = self.path / (job['id'] + '.' + uuid4().hex + '.tmp')
        try:
            with tmp.open('x', encoding='utf-8') as handle:
                raw = json.dumps(job, ensure_ascii=False, allow_nan=True,
                                 separators=(',', ':')).encode('utf-8')
                checksum = hashlib.sha256(raw).hexdigest()
                envelope = {'sha256': checksum, 'job': job}
                json.dump(envelope, handle, ensure_ascii=False, allow_nan=True,
                          separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_directory()
        finally:
            tmp.unlink(missing_ok=True)

    def plan(self, row, period, now):
        """Durably save original requested interval before starting HTTP."""
        job = {
            'version': 1,
            'id': uuid4().hex,
            'state': 'planned',
            'kind': 'initial',
            'company_id': int(row['id']),
            'symbol': str(row['ticker']).strip(),
            'latest_activity': str(row['last_activity'] or ''),
            'start': period.start.isoformat(),
            'end': period.end.isoformat(),
            'now': now.isoformat(),
        }
        with self._mutex:
            self._atomic_write(job)
        return job

    def ensure_repair(self, parent, start, end):
        """Persist one replayable gap request before ACK of initial work.

        Deterministic ID avoids spawning duplicate gap repair jobs if the
        original price batch is replayed after a crash at the commit boundary.
        """
        if parent.get('kind') != 'initial':
            raise SpoolError('Cannot recursively schedule price gap repair')
        job_id = uuid5(NAMESPACE_URL,
                       'massive-gap:' + parent['id'] + ':' +
                       start.isoformat() + ':' + end.isoformat()).hex
        with self._mutex:
            if self._path(job_id).exists():
                old = self.load(job_id)
                if (old.get('kind') != 'gap_repair' or old.get('parent') != parent['id']):
                    raise SpoolError('Conflicting persisted gap repair job')
                return job_id
            job = {**{k: parent[k] for k in (
                'version','company_id','symbol','latest_activity','now')},
                'id': job_id, 'state': 'planned', 'kind': 'gap_repair',
                'start': start.isoformat(), 'end': end.isoformat(),
                'original_start': parent['start'], 'original_end': parent['end'],
                'parent': parent['id']}
            self._atomic_write(job)
        return job_id

    def load(self, job_id):
        path = self._path(job_id)
        try:
            with path.open('r', encoding='utf-8') as handle:
                envelope = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SpoolError(f'Cannot read persisted price batch {path}: {exc}') from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get('job'), dict):
            raise SpoolError(f'Invalid persisted price batch envelope: {path}')
        job = envelope['job']
        raw = json.dumps(job, ensure_ascii=False, allow_nan=True,
                         separators=(',', ':')).encode('utf-8')
        if hashlib.sha256(raw).hexdigest() != envelope.get('sha256'):
            raise SpoolError(f'Price spool checksum mismatch; preserving {path}')
        if (job.get('version') != 1 or
                job.get('id') != job_id or job.get('state') not in ('planned', 'fetched') or
                job.get('kind', 'initial') not in ('initial', 'gap_repair') or
                not isinstance(job.get('symbol'), str) or not job['symbol'] or
                not isinstance(job.get('company_id'), int) or
                (job['state'] == 'fetched' and not isinstance(job.get('bars'), list))):
            raise SpoolError(f'Invalid persisted price batch: {path}')
        for field in ('start', 'end', 'now'):
            try:
                value = datetime.fromisoformat(job[field])
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError('missing timezone')
            except (KeyError, TypeError, ValueError) as exc:
                raise SpoolError(f'Invalid {field} in persisted price batch: {path}') from exc
        if job.get('kind') == 'gap_repair':
            for field in ('original_start', 'original_end'):
                try:
                    value = datetime.fromisoformat(job[field])
                    if value.tzinfo is None or value.utcoffset() is None:
                        raise ValueError('missing timezone')
                except (KeyError, TypeError, ValueError) as exc:
                    raise SpoolError(f'Invalid {field} in persisted price batch: {path}') from exc
        return job

    def pending_ids(self):
        # Load one response at a time; never materialize the entire backlog.
        return [p.stem for p in sorted(self.path.glob('*.json'))]

    def store_fetched(self, job_id, bars):
        with self._mutex:
            job = self.load(job_id)
            if job['state'] != 'planned':
                raise SpoolError('Cannot replace already-fetched price batch')
            if not isinstance(bars, list):
                raise SpoolError('Provider price response must be a list')
            job['bars'] = bars
            job['state'] = 'fetched'
            self._atomic_write(job)
        return job_id

    def complete(self, job_id):
        # The writer calls this only AFTER SQLite commit and validation.
        with self._mutex:
            path = self._path(job_id)
            path.unlink()
            self._fsync_directory()

    @contextmanager
    def exclusive_run(self):
        """One Massive ingestion process per database, including manual runs."""
        fd = os.open(self.path / '.writer.lock', os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.name == 'posix':
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise SpoolError('Another Massive price run already owns this database spool') from exc
            elif os.name == 'nt':
                import msvcrt
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise SpoolError('Another Massive price run already owns this database spool') from exc
            else:
                raise SpoolError('Unsupported OS for exclusive Massive price spool lock')
            locked = True
            yield self
        finally:
            if locked:
                if os.name == 'posix':
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)
