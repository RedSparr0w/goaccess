import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import gzip
import threading
from urllib.request import urlopen
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('dashboard_server', Path(__file__).parents[1] / 'dashboard/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class DashboardTests(unittest.TestCase):
    def test_filters_are_validated_and_combined(self):
        self.assertEqual(server.parse_filters('site=a.example&ip=192.0.2.1&url=%2Fx%3Fa%3Db'),
                         [('site', 'a.example'), ('ip', '192.0.2.1'), ('url', '/x?a=b')])
        for query in ['unknown=x', 'site=a&site=b', 'ip=%00', 'site=' + 'x' * 2049]:
            with self.assertRaises(ValueError):
                server.parse_filters(query)
        self.assertEqual(server.parse_filters('site=&ip='), [])

    def test_command_uses_fresh_storage_and_literal_arguments(self):
        with patch.dict(os.environ, {}, clear=True):
            command = server.Reports().command([('site', 'a; echo bad'), ('ip', '::1')], '/tmp/report')
        self.assertIn('--filter=site=a; echo bad', command)
        self.assertIn('--filter=ip=::1', command)
        self.assertNotIn('--persist', command)
        self.assertNotIn('--restore', command)
        self.assertIn('--no-global-config', command)

    def test_gzip_input_cache_and_filter_isolation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOG_PATH': directory}):
            root = Path(directory)
            (root / 'proxy-host-1_access.log').write_bytes(b'current')
            with gzip.open(root / 'proxy-host-1_access.log.1.gz', 'wb') as archive:
                archive.write(b'archive')
            (root / 'proxy-host-1_error.log').write_bytes(b'ignore')
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                self.assertEqual(kwargs['stdin'].read(), b'current\narchive\n')
                for arg in command:
                    if arg.startswith('--output='):
                        target = Path(arg.split('=', 1)[1])
                        target.write_text('<html>filtered</html>' if target.suffix == '.html' else json.dumps({'hosts': {'data': []}}))
                return type('Result', (), {'returncode': 0})()

            reports = server.Reports()
            with patch.object(server.subprocess, 'run', run):
                reports.get([])
                reports.get([])
                reports.get([('site', 'a.example')])
                reports.get([('site', 'b.example')])
            self.assertEqual(len(calls), 3)
            with patch.dict(os.environ, {'SKIP_ARCHIVED_LOGS': 'True'}):
                self.assertEqual(len(reports.files()), 1)

    def test_page_has_global_controls(self):
        page = server.page().decode()
        for field in server.FIELDS:
            self.assertIn('name="' + field + '"', page)
        self.assertIn('/report?', page)
        self.assertIn('All traffic', page)

    def test_http_routes_and_site_suggestions(self):
        class FakeReports:
            def get(self, filters):
                return json.dumps(filters).encode(), {
                    'vhosts': {'data': [{'data': 'one.example'}]},
                    'hosts': {'data': [{'data': '192.0.2.1'}]},
                }

        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.reports = FakeReports()
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        address = 'http://127.0.0.1:' + str(httpd.server_port)
        try:
            with urlopen(address + '/options') as response:
                self.assertEqual(json.load(response), {'site': ['one.example'], 'ip': ['192.0.2.1']})
            with urlopen(address + '/report?site=one.example&ip=192.0.2.1') as response:
                self.assertEqual(json.load(response), [['site', 'one.example'], ['ip', '192.0.2.1']])
            with self.assertRaises(HTTPError) as error:
                urlopen(address + '/report?unknown=x')
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
