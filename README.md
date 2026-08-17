# PJLink Exporter

A Prometheus exporter for PJLink-capable projectors and displays, written in Python.

[PJLink](https://pjlink.jbmia.or.jp/) is a cross-vendor standard protocol for projector/display
monitoring and control, supported by Epson's business/education projector line and most other
major projector brands. This exporter speaks it directly over a raw TCP socket — no vendor SDK,
no SNMP MIB — so the same exporter works across brands instead of being tied to one manufacturer.

## Usage

### Docker

#### Build

```bash
docker build -t pjlink-exporter .
```

#### Run

```bash
docker run -p 9878:9878 pjlink-exporter
```

### Manual

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

2.  Copy the example config and edit it:
    ```bash
    cp config.example.yaml config.yaml
    ```

3.  Run the exporter:
    ```bash
    python -m pjlink_exporter -c config.yaml
    ```

    CLI flags override config file values:
    ```bash
    python -m pjlink_exporter -c config.yaml --host 0.0.0.0 --port 9878 --log-level DEBUG
    ```

## Credentials (OpenBao)

The exporter looks up each device's PJLink password in [OpenBao](https://openbao.org/)
(Vault-compatible), keyed by the device hostname, so passwords don't have to live in the config
file or scrape URL. PJLink authenticates with a single shared password (no username).

### Secret layout

All devices live under one common KV **v2** path, with the device hostname as the final path
segment:

```bash
bao kv put secret/pjlink/192.168.1.10 password=secret
```

Here the mount is `secret`, the prefix is `pjlink`, and `192.168.1.10` is the device hostname.

### Configuration

Configure the exporter via environment variables (`BAO_*` take precedence, `VAULT_*` are accepted
as fallbacks so Nomad's native integration vars work directly):

| Variable | Default | Description |
|----------|---------|-------------|
| `BAO_ADDR` / `VAULT_ADDR` | — | OpenBao server address |
| `BAO_TOKEN` / `VAULT_TOKEN` | — | Token used to authenticate to OpenBao |
| `BAO_KV_MOUNT` | `secret` | KV v2 mount point |
| `BAO_PATH_PREFIX` | (empty) | Common path under the mount before the hostname |
| `BAO_CACERT` / `VAULT_CACERT` | — | Optional CA bundle for TLS verification |

If the OpenBao lookup returns nothing (not configured, secret missing, network error), the
exporter falls back to the device's `password` field in `config.yaml`, or the `password` URL
query parameter — fine for a device with no PJLink password set, less fine for anything you'd
rather not commit to a config file.

### Nomad

```hcl
vault {}

template {
  data        = "VAULT_TOKEN={{ env \"VAULT_TOKEN\" }}"
  destination = "secrets/bao.env"
  env         = true
}
```

## Metrics

The exporter exposes device metrics on the `/probe` endpoint.

**Config-file mode** — scrapes every device listed in `config.yaml`:

```bash
curl "http://localhost:9878/probe"
```

**URL-parameter mode** — probes a single device by query parameter, no config-file entry needed
(useful for driving many devices via Prometheus `relabel_configs`, blackbox-exporter style):

```bash
curl "http://localhost:9878/probe?host=192.168.1.10&name=room-101"
```

Both modes are always available; a request with a `host` parameter always wins for that request.

### Exposed Metrics

-   `probe_success`: 1 if the device responded to PJLink queries, 0 otherwise.
-   `probe_duration_seconds`: How long the probe took to complete.
-   `pjlink_power_status`: Power state from the PJLink `POWR` query. `0`=off, `1`=on, `2`=cooling, `3`=warm-up.
-   `pjlink_error_status` (labels: `component`): Error state per component from `ERST` — `fan`, `lamp`, `temperature`, `cover_open`, `filter`, `other`. `0`=ok, `1`=warning, `2`=error.
-   `pjlink_lamp_hours` (labels: `lamp`): Cumulative lamp usage hours, from `LAMP`. Most projectors have a single lamp (`lamp="0"`); PJLink supports up to eight.
-   `pjlink_lamp_on` (labels: `lamp`): 1 if that lamp is currently lit, 0 otherwise.
-   `pjlink_filter_hours`: Cumulative air filter usage hours, from the PJLink Class 2 `FILT` query. Only present for devices that support Class 2 — check `pjlink_device_info{pjlink_class="2"}`. Class 1 devices only expose filter *warning/error* state via `pjlink_error_status{component="filter"}`, not hours.
-   `pjlink_device_info` (info metric, labels: `name`, `manufacturer`, `product_name`, `other_info`, `pjlink_class`): Device identity reported over PJLink (`NAME`/`INF1`/`INF2`/`INFO`/`CLSS`). Registered as `pjlink_device` in code — `InfoMetricFamily` appends the `_info` suffix in the exposition format. `other_info` (`INFO` query) is free text, not standardized by PJLink — most vendors, Epson included, put the firmware version there.

All device metrics carry `device` (the configured name) and `host` labels.

### Exporter self-metrics

`/metrics` returns the exporter's own process metrics (not any device's), for monitoring the
exporter itself: `pjlink_exporter_probe_requests_total`, `pjlink_exporter_probe_duration_seconds`,
plus the default Python/process collectors.

## Notes

- Devices are probed fresh on every scrape — no caching, no persistent connections. A fresh TCP
  connection is opened per probe since PJLink servers may drop idle connections.
- A device that doesn't implement an optional query (e.g. a Class 1 device asked for `FILT`)
  simply omits that metric rather than failing the whole probe — check `pjlink_device` info
  labels or `pjlink_class` to know what a given device should be expected to report.
