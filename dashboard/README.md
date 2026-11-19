# Indexed filtering in the native GoAccess report

The native report contains website, client IP, method and status filters, with
URL, referrer, user agent, protocol and remote user under More filters. There is
no iframe or individual table search UI. Filters combine with AND and update
all traffic panels, charts and totals together.

Website, IP and status inputs have searchable dropdowns. Focus an input to see
values; type a prefix to search, then click a value or use arrow keys and Enter.
Choose All to clear that field. Suggestions query the complete request index,
not the report's top rows. Each search returns up to 100 values; type more to
narrow it. Free text and Apply also work. Method selection applies immediately.

## Deployment

Push changes, then replace the server Compose file with `dashboard/compose.yml`.
It retains port 7880 and the NPM log mount and adds a **goaccess_index** volume:

```sh
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose logs -f goaccess
```

Reload the browser to load the new inputs. The index volume stores SQLite data
and cached reports across container recreation. Keep it for subsequent builds.
The first startup imports available logs before serving requests; later startups
check file metadata and reuse indexed history. Allow disk space for the index
and copies of raw request lines. The old aggregate cache is not used or deleted.

## Data processing and speed

A background worker checks logs every five seconds. It reads only appended
complete lines from live files. Unchanged gzip archives are not reopened.
Rotation, truncation, replacement and removal update the index transactionally.
Incomplete live lines are retried on the next pass. Failed imports retain the
previous transaction rather than publishing a partial update.

SQLite indexes site/IP (together), IP, status and method. A site/IP selection
queries matching stored requests directly, without scanning or decompressing the
original logs. Substring filters on URL, agent and referrer can scan the request
index. These filters are case-sensitive; site/IP/method/status match exactly.

GoAccess still parses and aggregates the **selected** requests to retain the
existing panel semantics. This is not a fully preaggregated analytical engine:
large selections, including all traffic, can still take time on a cache miss.
Rendered results are cached on disk by index generation and configuration and
reused until ingestion changes the dataset, including across restarts. Up to 16
cached outputs are retained. Browser selections are cached for 30 seconds.
Refresh forces an ingestion check and bypasses report caches.

Only NPM proxy access logs in the default format are supported by this indexed
backend. The initial import reads the full historical input once. Historical
traffic must remain in the mounted logs; removed files are removed from the
index on ingestion. `KEEP_LAST` limits report dates but does not remove indexed
raw history. NPM's default format does not include protocol version or remote
user; those filters cannot match absent fields.

Responses use gzip. Server-Timing headers and container logs separate queue,
indexed query, GoAccess aggregation, output and compression time. The browser
console reports native rendering time. The loading overlay is removed entirely
once the report initializes so it cannot intercept input clicks.

## Settings

- `LOG_PATH`: directory containing NPM logs (default `/opt/log`).
- `LOG_PATTERN`: default `proxy-host-*_access.log*`.
- `SKIP_ARCHIVED_LOGS=True`: ignore files not ending in `.log`.
- `INDEX_PATH`: SQLite path; Compose uses `/var/cache/goaccess-index/requests.sqlite3`.
- `INDEX_INTERVAL`: ingestion interval in seconds (default 5).
- `INDEX_TIMEOUT`: ingestion deadline in seconds (default 3600).
- `KEEP_LAST`: optional number of days in generated reports.
- `REPORT_CACHE_SECONDS`: browser dataset cache lifetime (default 30).
- `REPORT_MAX_ITEMS`: displayed rows per panel (default 366), not an input filter.
- `REPORT_TIMEOUT`: selected request export and GoAccess deadline (default 180).
- `PORT`: default 7880.

There is no automatic report refresh; ingestion runs in the background, while
Refresh loads current traffic. Static HTML cannot recompute aggregates without
the `/data` endpoint. The native CLI also supports `--filter=site=example.com`
and other request filters, but requires fresh storage and cannot use --restore.
