import gzip
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
from request_index import RequestIndex


def line(site='one.example', ip='192.0.2.1', url='/one', status='200'):
    return (f'[01/Jul/2026:06:20:38 +0000] - - {status} - GET https {site} "{url}" [Client {ip}] [Length 100] [Gzip -] [Sent-to 127.0.0.1] "Agent" "https://ref/"\n').encode()


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = RequestIndex(self.root / 'index.sqlite3')
        self.log = self.root / 'proxy-host-1_access.log'

    def sync(self, files=None):
        return self.index.refresh(files if files is not None else [self.log], time.monotonic() + 30)

    def records(self, *filters):
        output = io.BytesIO()
        self.index.export(filters, output)
        return output.read()

    def test_incremental_append_partial_lines_and_restart(self):
        first, second = line(), line('two.example')
        self.log.write_bytes(first + second[:30])
        self.sync()
        self.assertEqual(self.records(), first)
        with self.log.open('ab') as stream:
            stream.write(second[30:])
        self.sync()
        self.assertEqual(self.records(), first + second)
        self.assertFalse(self.sync())
        restarted = RequestIndex(self.root / 'index.sqlite3')
        self.assertEqual(restarted.version(), self.index.version())
        self.assertEqual(restarted.suggestions('site'), ['one.example', 'two.example'])

    def test_archive_not_reopened_and_rotation_replaces_rows(self):
        archive = self.root / 'archive.gz'
        with gzip.open(archive, 'wb') as stream:
            stream.write(line('archived.example'))
        self.log.write_bytes(line())
        self.sync([self.log, archive])
        with patch('request_index.gzip.open', side_effect=AssertionError('archive reopened')):
            self.assertFalse(self.sync([self.log, archive]))
        self.log.write_bytes(line('changed.example'))
        self.sync([self.log])
        self.assertEqual(self.index.suggestions('site'), ['changed.example'])

    def test_indexed_combined_filters_substrings_and_suggestions(self):
        first = line(url='/one?a=b')
        self.log.write_bytes(first + line('two.example', '192.0.2.2', status='404'))
        self.sync()
        self.assertEqual(self.records(('site', 'one.example'), ('ip', '192.0.2.1'), ('url', '?a=b')), first)
        self.assertEqual(self.records(('site', 'missing')), b'')
        self.assertEqual(self.index.suggestions('ip', '192.0.2.'), ['192.0.2.1', '192.0.2.2'])
        self.assertEqual(self.index.suggestions('site', '%'), [])
        with self.index.connect() as db:
            plan = str(db.execute('EXPLAIN QUERY PLAN SELECT raw FROM requests WHERE site=? AND ip=?', ('one.example', '192.0.2.1')).fetchall())
        self.assertIn('USING INDEX request_site_ip', plan)
        with self.assertRaises(ValueError):
            self.index.suggestions('site; DROP TABLE requests')

    def test_failed_ingestion_rolls_back(self):
        self.log.write_bytes(line())
        self.sync()
        version = self.index.version()
        self.log.write_bytes(line('new.example'))
        with self.assertRaises(TimeoutError):
            self.index.refresh([self.log], 0)
        self.assertEqual(self.index.version(), version)
        self.assertEqual(self.index.suggestions('site'), ['one.example'])


if __name__ == '__main__':
    unittest.main()
