"""HTTP data backend for filters inside the native GoAccess report."""
import gzip
import hashlib
import logging
import sys
import sqlite3
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from request_index import RequestIndex

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
        build = hashlib.sha256(Path(__file__).read_bytes())
        binary = Path(os.getenv('GOACCESS_BIN', '/usr/bin/goaccess'))
        if binary.is_file():
            with binary.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    build.update(chunk)
        self.build_id = build.hexdigest()
        self.temporary = tempfile.TemporaryDirectory(prefix='goaccess-source-')
        self.source = Path(self.temporary.name) / 'access.log'
        self.index = RequestIndex(Path(os.getenv('INDEX_PATH', str(Path(self.temporary.name) / 'requests.sqlite3'))))
        self.disk_cache = Path(self.index.path).parent / "reports"
        self.disk_cache.mkdir(exist_ok=True)
        self.index_ready = False
        self.stop = threading.Event()
        self.worker = None

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join()
        self.temporary.cleanup()

    def update_index(self):
        if os.getenv('LOG_FORMAT', NPM_FORMAT) != NPM_FORMAT:
            raise ValueError('The indexed backend currently supports NPM proxy access logs only')
        self.index.refresh(self.files(), time.monotonic() + int(os.getenv('INDEX_TIMEOUT', '3600')))
        self.index_ready = True

    def start(self):
        self.update_index()
        def ingest():
            while not self.stop.wait(max(1, int(os.getenv('INDEX_INTERVAL', '5')))):
                try:
                    self.update_index()
                except Exception:
                    logging.exception('Incremental log ingestion failed; retaining the previous index')
        self.worker = threading.Thread(target=ingest, daemon=True)
        self.worker.start()

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

    def get(self, filters, output='html', refresh=False):
        if output not in ('html', 'json'):
            raise ValueError('Unsupported report format')
        started = time.monotonic()
        with self.lock:
            timings = {'queue': (time.monotonic() - started) * 1000}
            if not self.index_ready or refresh:
                self.update_index()
            key = (self.index.version(), tuple(filters), output)
            digest = hashlib.sha256(json.dumps((self.build_id, key, self.max_items, os.getenv('KEEP_LAST', ''), os.getenv('DATE_FORMAT', ''), os.getenv('TIME_FORMAT', ''))).encode()).hexdigest()
            disk = self.disk_cache / (digest + '.' + output)
            if not refresh and disk.exists():
                return disk.read_bytes(), dict(timings, cache=0)
            cached = self.cache.get(key)
            if not refresh and cached:
                return cached[1], dict(timings, cache=0)
            deadline = time.monotonic() + int(os.getenv('REPORT_TIMEOUT', '180'))
            stage = time.monotonic()
            with tempfile.TemporaryDirectory(prefix='goaccess-report-') as directory:
                with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as errors:
                    count = self.index.export(filters, source)
                    if not count and filters:
                        # Feed a format sample; native AND filters still produce zero hits.
                        self.index.export([], source, limit=1)
                    timings['query'] = (time.monotonic() - stage) * 1000
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
            disk.write_bytes(payload)
            for obsolete in sorted(self.disk_cache.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)[16:]:
                obsolete.unlink(missing_ok=True)
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
    config = json.dumps({'endpoint': '/data', 'suggestions': '/suggestions', 'cacheSeconds': reports.ttl,
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
            if url.path == '/suggestions':
                params = parse_qs(url.query, max_num_fields=4)
                if set(params) - {'field', 'q'}:
                    raise ValueError('Unknown suggestion parameter')
                field = params.get('field', ['site'])[0]
                query = params.get('q', [''])[0]
                if len(query) > 2048:
                    raise ValueError('Suggestion query too long')
                if not self.server.reports.index_ready:
                    self.server.reports.update_index()
                payload = self.server.reports.index.suggestions(field, query)
                self.send(200, json.dumps(payload).encode(), 'application/json')
                return
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
        except (ValueError, OSError, EOFError, sqlite3.Error, subprocess.TimeoutExpired) as error:
            status = 400 if isinstance(error, ValueError) else 503
            if url.path in ('/data', '/suggestions'):
                self.send(status, json.dumps({'error': str(error)}).encode(), 'application/json')
            else:
                body = ('<!doctype html><h2>Unable to generate report</h2><pre>'
                        + html.escape(str(error)) + '</pre>').encode()
                self.send(status, body, 'text/html; charset=utf-8')


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '7880'))), Handler)
    server.reports = Reports()
    server.reports.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.reports.close()
