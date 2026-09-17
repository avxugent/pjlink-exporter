"""
Prometheus collector for PJLink devices.

Probes one or more devices over PJLink and exposes connectivity, power/error
status, lamp hours, and filter hours as Prometheus metrics.
"""

from __future__ import annotations

import logging
from typing import Any, Generator, Optional

from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily, Metric

from .prober import ProbeResult, probe_device

logger = logging.getLogger(__name__)

# Mirrors the PJLink POWR command's raw codes; documented on the metric's
# HELP text too so it's visible from /probe output alone.
POWER_STATE_NAMES = {0: "off", 1: "on", 2: "cooling", 3: "warm-up"}


class PJLinkCollector:
    """
    A Prometheus custom collector that probes PJLink devices.

    Each call to :meth:`collect` probes every configured device fresh (no
    caching) and yields the results.
    """

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        """
        :param devices: List of device configuration dicts. Each dict must
            contain at least ``name`` and ``host``; optional keys are
            ``port``, ``password``, and ``timeout``.
        """
        self._devices = devices

    # ------------------------------------------------------------------
    # prometheus_client collector interface
    # ------------------------------------------------------------------

    def describe(self) -> Generator[Metric, None, None]:
        yield GaugeMetricFamily("probe_success", "")
        yield GaugeMetricFamily("probe_duration_seconds", "")
        yield GaugeMetricFamily("pjlink_power_status", "")
        yield GaugeMetricFamily("pjlink_error_status", "")
        yield GaugeMetricFamily("pjlink_lamp_hours", "")
        yield GaugeMetricFamily("pjlink_lamp_on", "")
        yield GaugeMetricFamily("pjlink_filter_hours", "")
        yield InfoMetricFamily("pjlink_device", "")

    def collect(self) -> Generator[Metric, None, None]:
        """Probe all devices and yield Prometheus metric families."""
        results: list[tuple[dict[str, Any], ProbeResult]] = [
            (dev_cfg, self._probe(dev_cfg)) for dev_cfg in self._devices
        ]
        base_labels = ["device", "host"]

        # --- probe_success / probe_duration_seconds ---
        success_family = GaugeMetricFamily(
            "probe_success",
            "1 if the device responded to PJLink queries, 0 otherwise.",
            labels=base_labels,
        )
        duration_family = GaugeMetricFamily(
            "probe_duration_seconds",
            "Duration of the probe in seconds.",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            labels = [dev_cfg["name"], dev_cfg["host"]]
            success_family.add_metric(labels, 1.0 if r.success else 0.0)
            duration_family.add_metric(labels, r.duration_seconds)
        yield success_family
        yield duration_family

        # --- pjlink_power_status ---
        power_family = GaugeMetricFamily(
            "pjlink_power_status",
            "Power state from the PJLink POWR query: "
            + ", ".join(f"{code}={name}" for code, name in POWER_STATE_NAMES.items()) + ".",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            if r.power_state is not None:
                power_family.add_metric(
                    [dev_cfg["name"], dev_cfg["host"]], float(r.power_state)
                )
        yield power_family

        # --- pjlink_error_status ---
        error_family = GaugeMetricFamily(
            "pjlink_error_status",
            "Error state per component from the PJLink ERST query: 0=ok, 1=warning, 2=error.",
            labels=base_labels + ["component"],
        )
        for dev_cfg, r in results:
            for component, code in r.errors.items():
                error_family.add_metric(
                    [dev_cfg["name"], dev_cfg["host"], component], float(code)
                )
        yield error_family

        # --- pjlink_lamp_hours / pjlink_lamp_on ---
        lamp_hours_family = GaugeMetricFamily(
            "pjlink_lamp_hours",
            "Cumulative lamp usage hours, from the PJLink LAMP query.",
            labels=base_labels + ["lamp"],
        )
        lamp_on_family = GaugeMetricFamily(
            "pjlink_lamp_on",
            "1 if the lamp is currently lit, 0 otherwise.",
            labels=base_labels + ["lamp"],
        )
        for dev_cfg, r in results:
            for index, (hours, is_on) in enumerate(r.lamps):
                labels = [dev_cfg["name"], dev_cfg["host"], str(index)]
                lamp_hours_family.add_metric(labels, float(hours))
                lamp_on_family.add_metric(labels, 1.0 if is_on else 0.0)
        yield lamp_hours_family
        yield lamp_on_family

        # --- pjlink_filter_hours (Class 2 only) ---
        filter_family = GaugeMetricFamily(
            "pjlink_filter_hours",
            "Cumulative air filter usage hours, from the PJLink Class 2 FILT query. "
            "Only present for devices that support it.",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            if r.filter_hours is not None:
                filter_family.add_metric(
                    [dev_cfg["name"], dev_cfg["host"]], float(r.filter_hours)
                )
        yield filter_family

        # --- pjlink_device (info) ---
        device_family = InfoMetricFamily(
            "pjlink_device",
            "Device identity reported over PJLink (NAME/INF1/INF2/INFO/CLSS).",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            info: dict[str, str] = {}
            if r.name:
                info["name"] = r.name
            if r.manufacturer:
                info["manufacturer"] = r.manufacturer
            if r.product_name:
                info["product_name"] = r.product_name
            if r.version:
                info["version"] = r.version
            if r.pjlink_class is not None:
                info["pjlink_class"] = str(r.pjlink_class)
            if info:
                device_family.add_metric([dev_cfg["name"], dev_cfg["host"]], info)
        yield device_family

    # ------------------------------------------------------------------
    # Device probing
    # ------------------------------------------------------------------

    def _probe(self, dev_cfg: dict[str, Any]) -> ProbeResult:
        """Probe a single device and return the result."""
        return probe_device(
            host=dev_cfg["host"],
            port=int(dev_cfg.get("port", 4352)),
            password=dev_cfg.get("password") or None,
            timeout=float(dev_cfg.get("timeout", 10.0)),
        )
