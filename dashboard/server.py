"""HTTP data backend for filters inside the native GoAccess report."""
import gzip
import html
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

FIELDS = ('site', 'ip', 'url', 'method', 'status', 'referrer', 'agent', 'protocol', 'user')
NPM_FORMAT = '[%d:%t %^] %^ %^ %s - %m %^ %v "%U" [Client %h] [Length %b] [Gzip %^] [Sent-to %^] "%u" "%R"'


def browser_data(value):
    """Match get_json(..., escape_html=1) used by native HTML/WebSocket output."""
    if isinstance(value, str):
        return html.escape(value, quote=False).replace("'", '&#39;')
    if isinstance(value, list):
        return [browser_data(item) for item in value]
    if isinstance(value, dict):
        return {key: browser_data(item) for key, item in value.items()}
    return value


class Reports:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.ttl = max(1, int(os.getenv('REPORT_CACHE_SECONDS', '30')))
        self.max_items = max(1, int(os.getenv('REPORT_MAX_ITEMS', '366')))
        self.temporary = tempfile.TemporaryDirectory(prefix='goaccess-source-')
        self.source = Path(self.temporary.name) / 'access.log'
        self.signature = None

    def close(self):
        self.temporary.cleanup()

    def command(self, filters, directory, output='html'):
        command = [os.getenv('GOACCESS_BIN', '/usr/bin/goaccess'), '-',
                   '--no-global-config', '--no-progress',
                   '--log-format=' + os.getenv('LOG_FORMAT', NPM_FORMAT),
                   '--date-format=' + os.getenv('DATE_FORMAT', '%d/%b/%Y'),
                   '--time-format=' + os.getenv('TIME_FORMAT', '%T'),
                   '--http-method=yes', '--http-protocol=yes',
                   '--max-items=' + str(self.max_items),
                   '--output=' + str(Path(directory) / ('report.' + output))]
        # Filtered reports always use fresh aggregation storage.
        command.extend('--filter=' + field + '=' + value for field, value in filters)
        keep_last = os.getenv('KEEP_LAST', '')
        if keep_last:
            command.append('--keep-last=' + str(int(keep_last)))
        return command

    def files(self):
        root = Path(os.getenv('LOG_PATH', '/opt/log'))
        pattern = os.getenv('LOG_PATTERN', 'proxy-host-*_access.log*')
        archived = os.getenv('SKIP_ARCHIVED_LOGS', 'False').lower() != 'true'
        return sorted(p for p in root.glob(pattern) if p.is_file()
                      and (archived or p.name.endswith('.log')))

    def prepare(self, files, deadline):
        signature = []
        for path in files:
            stat = path.stat()
            signature.append((str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns))
        if self.signature == signature:
            return
        # Invalidate first: a failed refresh must never publish a partial snapshot.
        self.signature = None
        with self.source.open('wb') as target:
            for path in files:
                opener = gzip.open if path.suffix == '.gz' else open
                with opener(path, 'rb') as stream:
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError('Timed out preparing access logs')
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                target.write(b'\n')
        self.signature = signature
        self.cache.clear()

    def get(self, filters, output='html', refresh=False):
        if output not in ('html', 'json'):
            raise ValueError('Unsupported report format')
        key = (tuple(filters), output)
        started = time.monotonic()
        with self.lock:
            timings = {'queue': (time.monotonic() - started) * 1000}
            cached = self.cache.get(key)
            if not refresh and cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1], dict(timings, cache=0)
            deadline = time.monotonic() + int(os.getenv('REPORT_TIMEOUT', '180'))
            stage = time.monotonic()
            files = self.files()
            if not files:
                raise ValueError('No access logs found. Check LOG_PATH and LOG_PATTERN.')
            self.prepare(files, deadline)
            timings['source'] = (time.monotonic() - stage) * 1000
            with tempfile.TemporaryDirectory(prefix='goaccess-report-') as directory:
                with self.source.open('rb') as source, tempfile.TemporaryFile() as errors:
                    stage = time.monotonic()
                    result = subprocess.run(self.command(filters, directory, output), stdin=source,
                                            stdout=subprocess.DEVNULL, stderr=errors,
                                            timeout=max(0.001, deadline - time.monotonic()))
                    timings['goaccess'] = (time.monotonic() - stage) * 1000
                    if result.returncode:
                        errors.seek(0)
                        raise ValueError('Report generation failed:\n' + errors.read(8192).decode('utf-8', 'replace'))
                stage = time.monotonic()
                payload = (Path(directory) / ('report.' + output)).read_bytes()
                if output == 'json':
                    payload = json.dumps(browser_data(json.loads(payload)), separators=(',', ':')).encode()
                timings['output'] = (time.monotonic() - stage) * 1000
            if len(self.cache) >= 8:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), payload)
            return payload, timings


def parse_filters(query):
    values = parse_qs(query, keep_blank_values=True, max_num_fields=16)
    if set(values) - set(FIELDS) - {'_refresh'}:
        raise ValueError('Unknown filter')
    if '_refresh' in values and values['_refresh'] != ['1']:
        raise ValueError('Invalid refresh parameter')
    filters = []
    for field in FIELDS:
        entries = values.get(field, [''])
        if len(entries) != 1 or len(entries[0]) > 2048 or '\x00' in entries[0]:
            raise ValueError('Invalid filter value')
        if entries[0]:
            filters.append((field, entries[0]))
    return filters


def native_page(report, reports):
    config = json.dumps({'endpoint': '/data', 'cacheSeconds': reports.ttl,
                         'maxItems': reports.max_items}, separators=(',', ':'))
    script = ('<script>window.goaccessFilterConfig=' + config + ';</script>').encode()
    if b'</body>' not in report:
        raise ValueError('GoAccess returned an incomplete HTML report')
    return report.replace(b'</body>', script + b'</body>', 1)


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, kind, timings=None):
        timings = dict(timings or {})
        encoding = None
        if 'gzip' in self.headers.get('Accept-Encoding', '').lower() and len(content) > 1024:
            stage = time.monotonic()
            content = gzip.compress(content, compresslevel=1)
            timings['compress'] = (time.monotonic() - stage) * 1000
            encoding = 'gzip'
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Vary', 'Accept-Encoding')
        if encoding:
            self.send_header('Content-Encoding', encoding)
        if timings:
            self.send_header('Server-Timing', ', '.join(f'{key};dur={value:.1f}' for key, value in timings.items()))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlsplit(self.path)
        try:
            if url.path not in ('/', '/data'):
                self.send(404, b'Not found', 'text/plain')
                return
            filters = parse_filters(url.query)
            refresh = '_refresh' in parse_qs(url.query)
            output = 'html' if url.path == '/' else 'json'
            payload, timings = self.server.reports.get(filters, output, refresh=refresh)
            if output == 'html':
                payload = native_page(payload, self.server.reports)
            kind = 'text/html; charset=utf-8' if output == 'html' else 'application/json'
            self.send(200, payload, kind, timings)
            self.log_message('report format=%s bytes=%d timing_ms=%s', output, len(payload), timings)
        except (ValueError, OSError, EOFError, subprocess.TimeoutExpired) as error:
            status = 400 if isinstance(error, ValueError) else 503
            if url.path == '/data':
                self.send(status, json.dumps({'error': str(error)}).encode(), 'application/json')
            else:
                body = ('<!doctype html><h2>Unable to generate report</h2><pre>'
                        + html.escape(str(error)) + '</pre>').encode()
                self.send(status, body, 'text/html; charset=utf-8')


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '7880'))), Handler)
    server.reports = Reports()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.reports.close()
