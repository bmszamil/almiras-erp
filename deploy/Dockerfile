# syntax=docker/dockerfile:1.7
# Almiras ERP application image: web (gunicorn), scheduler (supercronic) and the one-off migrate step all run
# from this image. Build from the repository root:
#   docker build -f deploy/Dockerfile -t almiras-erp:<git-sha> .
# Open source only. No secret is baked in: every secret arrives at run time as a file (see bin/entrypoint.sh).
#
# Optional build secret ``extra_ca``: a CA bundle for a TLS-intercepting proxy or a KSA-hosted package mirror
# (pip and the Go module download). Not needed for a direct build:
#   docker build --secret id=extra_ca,src=/path/ca.pem -f deploy/Dockerfile .

ARG PYTHON_IMAGE=python:3.13.16-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG GO_IMAGE=golang:1.26.9-trixie@sha256:f06b7122f888680048afb247c4e5f49c0069eb6c4c1ccae0f3f4834527a8d22c

# --- supercronic (MIT): cron for containers, built from source through the Go checksum database -------------
FROM ${GO_IMAGE} AS tools
ARG SUPERCRONIC_VERSION=v0.2.48
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=-trimpath
# hadolint ignore=DL3062  # versions are pinned through the build args above (@${..._VERSION})
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    go install -tags timetzdata -ldflags="-s -w" "github.com/aptible/supercronic@${SUPERCRONIC_VERSION}"

# --- application ----------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Riyadh \
    DJANGO_SETTINGS_MODULE=config.settings \
    ALMIRAS_METRICS_DIR=/var/lib/almiras/metrics

RUN groupadd --system --gid 10001 almiras \
    && useradd --system --uid 10001 --gid almiras --home-dir /app --no-create-home --shell /usr/sbin/nologin almiras

WORKDIR /app

# Exact versions with hashes; wheels only (no compiler in the image, no sdist build step).
COPY deploy/requirements.lock /tmp/requirements.lock
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY --from=tools /go/bin/supercronic /usr/local/bin/supercronic

# Code is owned by root and read-only for the runtime user.
COPY . /app
COPY deploy/bin/ /opt/almiras/bin/
COPY deploy/scheduler/crontab /opt/almiras/crontab
COPY deploy/postgres/sql/ /opt/almiras/sql/

# The metrics directory is shared (named volume) with the backup service (another uid): sticky, world-writable.
# Static files are collected at build time (no secret needed; DEBUG off as in production) and copied into the
# shared volume the reverse proxy serves when the web container starts (bin/entrypoint.sh web).
RUN chmod 0755 /opt/almiras/bin/* \
    && DJANGO_DEBUG=0 DJANGO_SECRET_KEY=collectstatic-at-build-time-only DJANGO_STATIC_ROOT=/opt/almiras/static \
       python manage.py collectstatic --noinput --verbosity 0 \
    && install -d -o almiras -g almiras -m 0750 /var/lib/almiras /srv/static \
    && install -d -m 1777 /var/lib/almiras/metrics \
    && python -m compileall -q /app > /dev/null

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/opt/almiras/bin/healthcheck.py"]

ENTRYPOINT ["/opt/almiras/bin/entrypoint.sh"]
CMD ["web"]
