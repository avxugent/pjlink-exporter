# syntax=docker/dockerfile:1

# ── Build / dependency stage ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into a separate prefix so we can copy them cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="pjlink-exporter" \
      org.opencontainers.image.description="Prometheus exporter for PJLink projectors and displays" \
      org.opencontainers.image.source="https://github.com/kristofkeppens/pjlink-exporter"

WORKDIR /app

# Copy installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy the application source
COPY pjlink_exporter/ ./pjlink_exporter/

# Bundle the example config as the built-in default.
# A custom config can be injected at runtime by mounting a file over this path
# or by setting PJLINK_CONFIG to point elsewhere.
RUN mkdir -p /etc/pjlink-exporter
COPY config.example.yaml /etc/pjlink-exporter/config.yaml

ENV PJLINK_CONFIG=/etc/pjlink-exporter/config.yaml

EXPOSE 9878

# Run as a non-root user for security
RUN useradd --no-create-home --shell /bin/false exporter
USER exporter

ENTRYPOINT ["python", "-m", "pjlink_exporter"]
