"""Incremental, persistent request index for NPM proxy access logs."""
from contextlib import contextmanager
import gzip
import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path

FIELDS = ('site', 'ip', 'url', 'method', 'status', 'referrer', 'agent', 'protocol', 'user')
PATTERN = re.compile(r'^\[[^]]+\] \S+ \S+ (?P<status>\d{3}) - (?P<method>\S+) \S+ (?P<site>\S+) "(?P<url>(?:\\.|[^"\\])*)" \[Client (?P<ip>[^]]+)\] \[Length \d+\] \[Gzip [^]]*\] \[Sent-to [^]]*\] "(?P<agent>(?:\\.|[^"\\])*)" "(?P<referrer>(?:\\.|[^"\\])*)"')


class RequestIndex:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, inode TEXT, size INTEGER,
                    mtime INTEGER, offset INTEGER, head TEXT);
                CREATE TABLE IF NOT EXISTS requests(source TEXT, raw BLOB, site TEXT, ip TEXT,
                    url TEXT, method TEXT, status TEXT, referrer TEXT, agent TEXT,
                    protocol TEXT, user TEXT);
                CREATE INDEX IF NOT EXISTS request_source ON requests(source);
                CREATE INDEX IF NOT EXISTS request_site_ip ON requests(site, ip);
                CREATE INDEX IF NOT EXISTS request_ip ON requests(ip);
                CREATE INDEX IF NOT EXISTS request_status ON requests(status);
                CREATE INDEX IF NOT EXISTS request_method ON requests(method);
                CREATE INDEX IF NOT EXISTS request_protocol ON requests(protocol);
                CREATE INDEX IF NOT EXISTS request_user ON requests(user);
                CREATE TABLE IF NOT EXISTS state(version INTEGER);
                INSERT INTO state SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM state);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=180)
        try:
            with db:
                yield db
        finally:
            db.close()

    def version(self):
        with self.connect() as db:
            return db.execute('SELECT version FROM state').fetchone()[0]

    def refresh(self, files, deadline):
        changed = False
        with self.lock, self.connect() as db:
            known = {row[0]: row for row in db.execute('SELECT * FROM sources')}
            present = {str(path) for path in files}
            for removed in known.keys() - present:
                db.execute('DELETE FROM requests WHERE source=?', (removed,))
                db.execute('DELETE FROM sources WHERE path=?', (removed,))
                changed = True
            for path in files:
                stat = path.stat()
                name = str(path)
                previous = known.get(name)
                if previous and previous[1:4] == (str(stat.st_ino), stat.st_size, stat.st_mtime_ns):
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError('Request indexing timed out')
                opener = gzip.open if path.suffix == '.gz' else open
                with opener(path, 'rb') as stream:
                    # Fingerprint the already consumed prefix to detect copy/truncate.
                    length = min(previous[4], 256) if previous else 256
                    prefix = stream.read(length)
                    head = hashlib.sha256(prefix).hexdigest()
                    append = (previous and path.suffix != '.gz' and str(stat.st_ino) == previous[1]
                              and stat.st_size >= previous[2] and head == previous[5])
                    offset = previous[4] if append else 0
                    if not append:
                        db.execute('DELETE FROM requests WHERE source=?', (name,))
                    stream.seek(offset)
                    batch = []
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError('Request indexing timed out')
                        raw = stream.readline()
                        if not raw or (not raw.endswith(b'\n') and path.suffix != '.gz'):
                            break
                        offset = stream.tell()
                        match = PATTERN.match(raw.decode('utf-8', 'replace'))
                        values = match.groupdict() if match else {}
                        batch.append((name, raw, *(values.get(field) for field in FIELDS)))
                        if len(batch) >= 5000:
                            db.executemany('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)', batch)
                            batch.clear()
                    if batch:
                        db.executemany('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)', batch)
                    stream.seek(0)
                    head = hashlib.sha256(stream.read(min(offset, 256))).hexdigest()
                    db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)',
                               (name, str(stat.st_ino), stat.st_size, stat.st_mtime_ns, offset, head))
                    changed = True
            if changed:
                db.execute('UPDATE state SET version=version+1')
        return changed

    def where(self, filters):
        clauses, values = [], []
        for field, value in filters:
            if field not in FIELDS:
                raise ValueError('Unknown request field')
            clauses.append('instr(' + field + ', ?) > 0' if field in ('url', 'referrer', 'agent') else field + ' = ?')
            values.append(value)
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), values

    def export(self, filters, target, limit=None):
        where, values = self.where(filters)
        count = 0
        with self.connect() as db:
            for (raw,) in db.execute('SELECT raw FROM requests' + where + (' LIMIT ?' if limit else ''), values + ([limit] if limit else [])):
                target.write(raw if raw.endswith(b'\n') else raw + b'\n')
                count += 1
        target.seek(0)
        return count

    def suggestions(self, field, query='', limit=100):
        if field not in ('site', 'ip', 'method', 'status', 'protocol', 'user'):
            raise ValueError('Unsupported suggestion field')
        # A binary prefix range uses the field's covering index, even for a
        # value near the end of a large IP list. SQL metacharacters stay literal.
        with self.connect() as db:
            return [row[0] for row in db.execute(
                f"SELECT DISTINCT {field} FROM requests WHERE {field} >= ? AND {field} < ? ORDER BY {field} LIMIT ?",
                (query, query + '\U0010ffff', limit))]
