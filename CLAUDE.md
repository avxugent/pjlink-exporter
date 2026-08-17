# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Prometheus exporter for PJLink-capable projectors and displays — availability, power/error
status, lamp hours, and filter hours. [PJLink](https://pjlink.jbmia.or.jp/) is a cross-vendor
standard (JBMIA) supported by Epson's business/education projector line and most other major
projector brands, so this exporter speaks it directly over a raw TCP socket (port 4352) rather
than a vendor SDK or SNMP MIB — one exporter works across brands instead of being tied to Epson
specifically. Started out targeting Epson only; renamed to `pjlink-exporter` once it was clear
the protocol itself is generic.

Pure-Python stdlib HTTP server (`http.server.ThreadingHTTPServer`) for the exporter's own HTTP
surface — no web framework. The PJLink side is a plain `socket` client (see below), not HTTP at
all.

Sibling exporters in this fleet: `pearl-exporter` (Sanic + HTTP API, Epiphan Pearl) and
`extronsis-exporter` (stdlib http.server, HTML scraping, Extron). This repo follows
extronsis-exporter's architecture most closely (package layout, config/URL dual-mode `/probe`,
stdlib server) since PJLink — like extronsis's HTML scraping — needs no async HTTP client.
Credential handling (`bao.py`) is carried over from pearl-exporter's OpenBao pattern, trimmed to
a single password field since PJLink has no username.

## Commands

```bash
# Install deps (PyYAML, prometheus-client, hvac)
pip install -r requirements.txt

# Run locally
cp config.example.yaml config.yaml   # edit devices
python -m pjlink_exporter -c config.yaml

# CLI flags override config file values
python -m pjlink_exporter -c config.yaml --host 0.0.0.0 --port 9878 --log-level DEBUG

# Docker
docker build -t pjlink-exporter .
docker compose up -d
```

There is **no test suite, linter, or type-checker configured** in this repo (no `pytest`, no
`pyproject.toml`, no CI lint step — matching extronsis-exporter's setup). The only CI job
(`.github/workflows/docker.yml`) builds and pushes a multiarch Docker image to Docker Hub on push
to `main`. Verify changes manually by running the exporter and curling its endpoints (see below).
`prober.py`'s `probe_device()` needs a real or fake PJLink TCP server to exercise — there's no
pure-function entry point analogous to extronsis's `extract_version()`; a loopback fake-server
fixture (canned greeting + responses over a `socket.socketpair()` or a background
`socketserver.TCPServer`) is the way to unit test it if a test suite gets added.

## Architecture

Four files under `pjlink_exporter/`, each with a single responsibility:

- **`prober.py`** — `probe_device()`: opens a raw TCP socket to the device, does the PJLink
  handshake (reads the `PJLINK 0`/`PJLINK 1 <seed>` greeting, computes the `md5(seed+password)`
  auth prefix if required), then issues a fixed sequence of queries (`CLSS`, `POWR`, `ERST`,
  `LAMP`, `FILT` if Class 2, `NAME`/`INF1`/`INF2`) and returns a `ProbeResult`. Every command and
  response is `\r`-terminated, not `\n`. The auth prefix is only prepended to the *first* command
  sent after the greeting — PJLink authenticates the whole TCP connection, not each command
  individually; `_PJLinkSession` tracks this with `_first_command_sent`. `success` on the result
  means the device responded to the mandatory `POWR` query — every other query (`ERST`, `LAMP`,
  `FILT`, device-info) is best-effort and independently wrapped so one device declining an
  optional command with `ERR1` ("undefined command") doesn't fail the whole probe. A fresh
  connection is opened per probe rather than pooled, since PJLink servers may drop idle
  connections and scrapes are infrequent.

  `CLSS` failing (some Class 1 devices don't implement it) is treated as "assume Class 1" rather
  than a probe failure — `FILT` (filter hours) is only attempted for devices that reported Class
  2, since Class 1 devices don't have it at all (they only expose the filter *warning/error* bit
  via `ERST`, no hour count).

- **`collector.py`** — `PJLinkCollector(prometheus_client.registry.Collector)`: turns
  `ProbeResult`s into Prometheus metric families (`probe_success`, `probe_duration_seconds`,
  `pjlink_power_status`, `pjlink_error_status`, `pjlink_lamp_hours`, `pjlink_lamp_on`,
  `pjlink_filter_hours`, `pjlink_device` info). `collect()` runs fresh on every scrape (no
  caching), probing every configured device sequentially. Per-device failures don't abort the
  whole scrape — every device gets a `probe_success` value regardless of whether others failed.
  Purely a protocol-result-to-metric mapper; it takes device dicts with credentials already
  resolved and has no OpenBao/config-fallback logic of its own (that lives in `__main__.py`, see
  below) — kept this way so the collector stays testable without touching `bao.py`.

- **`bao.py`** — OpenBao password lookup, `get_password(hostname)`. Trimmed from
  pearl-exporter's `bao.py` (drops the username half, since PJLink auth is a single shared
  password). Returns `None` on any failure (missing config/token, secret not found, network) —
  never logs secret values.

- **`__main__.py`** — config loading (`load_config`, YAML + defaults merge), credential
  resolution (`_resolve_passwords`: OpenBao first per device by hostname, falling back to the
  config file's/URL's `password` field), the HTTP handler (`_make_handler` closes over the
  loaded `cfg` dict), and `main()`/CLI argument parsing. Routes: `/` (HTML index), `/probe`,
  `/metrics`, `/healthz`.

### The two probing modes (both always active, can combine)

- **Config-file mode**: plain `GET /probe` scrapes every device listed under `devices:` in the
  YAML config.
- **URL-parameter mode**: `GET /probe?host=...&name=...` scrapes a single device described
  entirely by query params — no config-file entry needed. Intended for driving many devices via
  Prometheus relabeling, blackbox-exporter style. `_device_from_query_params()` in `__main__.py`
  builds the device dict; only `host` is required, everything else falls back to
  `_DEVICE_DEFAULTS`.

If a request supplies `host` as a query param, URL-parameter mode wins for that request even if
`devices:` is also configured — the two modes are per-request, not mutually exclusive globally.

### `/metrics` vs `/probe` — do not confuse these

- `/probe` returns **device** metrics (`probe_*`, `pjlink_*`), built from a fresh
  `CollectorRegistry` + `PJLinkCollector` per request.
- `/metrics` returns the **exporter's own** self-metrics (`pjlink_exporter_probe_requests_total`,
  `pjlink_exporter_probe_duration_seconds`, plus default process/platform collectors) from
  `prometheus_client`'s global `REGISTRY`. It does not touch any device.

Note the `probe_*` naming on `/probe` intentionally follows `blackbox_exporter` convention
(`probe_success`, `probe_duration_seconds`) since that's what users scraping this exporter will
already expect.

### Adding a new metric

1. If it requires a new PJLink query, add it to the sequence in `probe_device()` in `prober.py`
   and a new field on `ProbeResult`. Wrap it in `try/except LookupError` (plus `ValueError`/
   `IndexError` if parsing multi-value responses) unless it's mandatory — most PJLink commands
   beyond `POWR` are optional per-device.
2. Yield a new `GaugeMetricFamily`/`InfoMetricFamily` in `PJLinkCollector.collect()`, and add the
   matching empty-family stub in `describe()`.
3. Update the metrics table in `README.md`.

`InfoMetricFamily(name, ...)` renders as `<name>_info` in the exposition format — the family is
registered as `"pjlink_device"` in `collector.py` but shows up as `pjlink_device_info` in scrape
output.

## Configuration

`config.yaml` is gitignored (may contain internal hostnames, and a plaintext `password` fallback
if OpenBao isn't set up) — `config.example.yaml` is the tracked template and also what gets baked
into the Docker image as the built-in default (`/etc/pjlink-exporter/config.yaml`, overridable
via the `PJLINK_CONFIG` env var or by bind-mounting over that path). The `devices:` list is
optional at every level — an exporter with zero configured devices is valid and simply requires
every `/probe` call to pass `host`.

## Protocol notes worth knowing before touching `prober.py`

- Port 4352, `\r`-terminated lines, strict request/response (no pipelining).
- Greeting: `PJLINK 0` (no auth) / `PJLINK 1 <8-char-seed>` (password required) / `PJLINK ERRA`
  (device rejected the connection outright, e.g. too many failed attempts) — the last one is
  distinct from a normal auth-required greeting and is raised as `PermissionError` immediately.
- `POWR` codes: `0`=off, `1`=on, `2`=cooling, `3`=warm-up.
- `ERST` is six digits in a fixed order — fan, lamp, temperature, cover-open, filter, other —
  each `0`=ok/`1`=warning/`2`=error. This is the *only* source of filter status for Class 1
  devices (no hour count, just the warning/error bit).
- `LAMP` is space-separated `<hours> <on/off>` pairs, one pair per lamp, up to 8 lamps. Most
  Epson projectors report exactly one.
- `FILT` (filter usage hours) is Class 2 only — query `CLSS` first to know whether to attempt it.
- Device-side `ERRx` responses (`ERR1`=undefined command, `ERR2`=out of parameter,
  `ERR3`=unavailable right now, `ERR4`=projector/display failure) are raised as `LookupError` in
  `_PJLinkSession.query()` and caught per-query in `probe_device()` — they mean "this device
  doesn't support/can't currently answer this particular query," not "the probe failed."
