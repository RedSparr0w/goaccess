import importlib.util
import gzip
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('report_server', Path(__file__).parents[1] / 'dashboard/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class DashboardTests(unittest.TestCase):
    def reports(self):
        reports = server.Reports()
        self.addCleanup(reports.close)
        return reports

    def test_filters_are_validated_and_combined(self):
        self.assertEqual(server.parse_filters('site=a.example&ip=192.0.2.1&url=%2Fx%3Fa%3Db'),
                         [('site', 'a.example'), ('ip', '192.0.2.1'), ('url', '/x?a=b')])
        for query in ['unknown=x', 'site=a&site=b', 'ip=%00', 'site=' + 'x' * 2049, '_refresh=0']:
            with self.assertRaises(ValueError):
                server.parse_filters(query)
        self.assertEqual(server.parse_filters('site=&ip=&_refresh=1'), [])

    def test_one_output_and_bounded_rows(self):
        with patch.dict(os.environ, {}, clear=True):
            reports = self.reports()
            command = reports.command([('site', 'a; echo bad'), ('ip', '::1')], '/tmp/report', 'json')
        self.assertIn('--filter=site=a; echo bad', command)
        self.assertIn('--filter=ip=::1', command)
        self.assertNotIn('--persist', command)
        self.assertNotIn('--restore', command)
        self.assertIn('--no-global-config', command)
        self.assertIn('--max-items=366', command)
        self.assertEqual([arg for arg in command if arg.startswith('--output=')],
                         ['--output=' + str(Path('/tmp/report') / 'report.json')])

    def test_snapshot_reuse_rotation_refresh_and_filter_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOG_PATH': directory}, clear=True):
            root = Path(directory)
            current = root / 'proxy-host-1_access.log'
            current.write_bytes(b'current')
            archive = root / 'proxy-host-1_access.log.1.gz'
            with gzip.open(archive, 'wb') as stream:
                stream.write(b'archive')
            (root / 'proxy-host-1_error.log').write_bytes(b'ignore')
            calls = []
            sources = []

            def run(command, **kwargs):
                calls.append(command)
                sources.append(kwargs['stdin'].read())
                outputs = [Path(arg.split('=', 1)[1]) for arg in command if arg.startswith('--output=')]
                self.assertEqual(len(outputs), 1)
                outputs[0].write_text('<html><body>native</body></html>' if outputs[0].suffix == '.html' else json.dumps({'general': {'valid_requests': 1}, 'hosts': {'data': []}}))
                return type('Result', (), {'returncode': 0})()

            reports = self.reports()
            with patch.object(server.subprocess, 'run', run), patch.object(server.gzip, 'open', wraps=gzip.open) as decompressor:
                reports.get([])
                reports.get([])
                reports.get([('site', 'a.example')], 'json')
                reports.get([('site', 'b.example')], 'json')
                self.assertEqual(len(calls), 3)
                self.assertEqual(decompressor.call_count, 1)
                self.assertTrue(all(source == b'current\narchive\n' for source in sources))
                current.write_bytes(b'rotated')
                reports.get([], refresh=True)
                self.assertEqual(sources[-1], b'rotated\narchive\n')
                self.assertEqual(decompressor.call_count, 2)
                # Rebuild invalidates filtered results from the old log snapshot.
                reports.get([('site', 'a.example')], 'json')
                self.assertEqual(len(calls), 5)
                reports.get([('site', 'a.example')], 'json', refresh=True)
                self.assertEqual(len(calls), 6)
                self.assertEqual(decompressor.call_count, 2)
            with patch.dict(os.environ, {'SKIP_ARCHIVED_LOGS': 'True'}):
                self.assertEqual(len(reports.files()), 1)

    def test_json_matches_native_html_escaping(self):
        self.assertEqual(server.browser_data({'data': ["<script>&'", 12]}),
                         {'data': ['&lt;script&gt;&amp;&#39;', 12]})

    def test_native_page_has_no_wrapper(self):
        reports = self.reports()
        page = server.native_page(b'<html><body><main id="panels"></main></body></html>', reports)
        self.assertIn(b'goaccessFilterConfig', page)
        self.assertIn(b'id="panels"', page)
        self.assertNotIn(b'<iframe', page)
        self.assertNotIn(b'/options', page)
        self.assertNotIn(b'<form', page)

    def test_http_serves_native_html_and_json_with_timings_and_compression(self):
        calls = []

        class FakeReports:
            ttl = 30
            max_items = 366

            def get(self, filters, output, refresh=False):
                calls.append((filters, output, refresh))
                content = b'<html><body>' + b'x' * 2000 + b'</body></html>' if output == 'html' else json.dumps({'general': {'valid_requests': 1}, 'filters': filters}).encode()
                return content, {'goaccess': 18.0}

        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.reports = FakeReports()
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        address = 'http://127.0.0.1:' + str(httpd.server_port)
        try:
            request = Request(address + '/', headers={'Accept-Encoding': 'gzip'})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                self.assertIn('goaccess;dur=18.0', response.headers['Server-Timing'])
                self.assertIn(b'goaccessFilterConfig', gzip.decompress(response.read()))
            # Loading the page does not start a second unfiltered report for suggestions.
            self.assertEqual(calls, [([], 'html', False)])
            with urlopen(address + '/data?site=one.example&ip=192.0.2.1&_refresh=1', timeout=5) as response:
                self.assertEqual(json.load(response)['filters'], [['site', 'one.example'], ['ip', '192.0.2.1']])
            self.assertEqual(calls[-1][1:], ('json', True))
            for path in ['/options', '/report', '/data?unknown=x']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(address + path, timeout=5)
                self.assertEqual(error.exception.code, 400 if '?' in path else 404)
                error.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
