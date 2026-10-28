"""Same-sink serialization; all database writes are disposable test receipts."""
import psycopg
import pytest
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
