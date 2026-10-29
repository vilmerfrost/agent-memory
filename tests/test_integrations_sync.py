"""Same-sink serialization; all database writes are disposable test receipts."""
import psycopg
import pytest
from datetime import datetime, timezone, timedelta
from agent_memory.integrations import sync
from agent_memory.integrations.evidence import Health


class Sink:
    name = 'serialization-test'
    calls = 0
    def health(self):
        self.calls += 1
        return Health(True)
    def batches(self, conn, marks, *, replay=False):
        return iter(())


def test_same_sink_job_cannot_overwrite_a_concurrent_cursor(pg_url):
    with psycopg.connect(pg_url) as first, psycopg.connect(pg_url) as second:
        key = 'amem:sync:serialization-test'
        assert first.execute('select pg_try_advisory_lock(hashtextextended(%s, 0))', (key,)).fetchone()[0]
        sink = Sink()
        try:
            report = sync.run(sink, second, environ={})
            assert report.status == 'unavailable' and sink.calls == 0
            assert 'another sync' in report.detail
        finally:
            first.execute('select pg_advisory_unlock(hashtextextended(%s, 0))', (key,))
            first.commit()
        assert sync.run(sink, second, environ={}).status == 'ok'
        assert sink.calls == 1
        second.execute("delete from rsi_checkpoints where job='sync:serialization-test'");second.commit()


def test_sink_exception_releases_advisory_lock(pg_url):
    class Broken(Sink):
        def batches(self, conn, marks, *, replay=False):
            conn.execute('select * from nonexistent_export_fixture_table')
    with psycopg.connect(pg_url) as first, psycopg.connect(pg_url) as second:
        with pytest.raises(psycopg.errors.UndefinedTable):
            sync.run(Broken(), first, environ={})
        assert sync.run(Sink(), second, environ={}).status == 'ok'
        second.execute("delete from rsi_checkpoints where job='sync:serialization-test'");second.commit()


def test_backoff_and_dead_work_cannot_report_ok_or_advance_last_success(pg_url):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    previous = '2026-10-28T00:00:00+00:00'
    class Waiting(Sink):
        name = 'completeness-waiting'
    with psycopg.connect(pg_url) as conn:
        sync._save(conn, Waiting.name, 'done', {'last_ok_at': previous, 'failures': {
            'scope': {'attempts': 1, 'next_retry_at': (now + timedelta(hours=1)).isoformat()}}})
        conn.commit()
        report = sync.run(Waiting(), conn, now=now, environ={})
        assert report.status == 'partial' and 'scope' in report.persistent_failures
        assert sync.load(conn, Waiting.name)['last_ok_at'] == previous


def test_one_failed_record_does_not_block_sibling_and_failure_stays_visible(pg_url):
    class Separate(Sink):
        name = 'completeness-records'
        def batches(self, conn, marks, *, replay=False):
            for key in ('bad', 'good'):
                yield sync.Batch('scope', [{'id': key}], key, key=key, failure_key='scope:'+key)
        def push(self, batch):
            if batch.key == 'bad':
                raise ConnectionError('temporary fixture failure')
    with psycopg.connect(pg_url) as conn:
        report = sync.run(Separate(), conn, environ={})
        assert report.status == 'partial' and report.rows == 1
        assert set(report.persistent_failures) == {'scope:bad'}
        assert sync.load(conn, Separate.name)['watermarks']['scope'] == 'good'


def test_pending_processing_and_existing_dead_letters_are_incomplete(pg_url):
    class Pending(Sink):
        name = 'completeness-pending'
        def completion(self, conn):
            return {'completed': 9, 'queued': 2}
    with psycopg.connect(pg_url) as conn:
        sync._save(conn, Pending.name, 'done', {'dead_letters': [{'table': 'scope', 'key': 'bad'}]})
        conn.commit()
        report = sync.run(Pending(), conn, environ={})
        assert report.status == 'partial' and report.pending_rows == 2
        assert report.outstanding_dead_letters == 1
        assert 'last_ok_at' not in sync.load(conn, Pending.name)


def test_checkpoint_progress_is_committed_with_delivered_batch(pg_url):
    class Progress(Sink):
        name = 'completeness-progress'
        def batches(self, conn, marks, *, replay=False):
            yield sync.Batch('sessions', [{'id': 'one'}], 'mark')
        def push(self, batch):
            pass
        def checkpoint(self, batch, marks):
            marks['schedule'] = 'next-table'
    with psycopg.connect(pg_url) as conn:
        assert sync.run(Progress(), conn, environ={}).status == 'ok'
        assert sync.load(conn, Progress.name)['watermarks'] == {'sessions': 'mark', 'schedule': 'next-table'}
