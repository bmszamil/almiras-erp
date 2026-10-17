# syntax=docker/dockerfile:1.7
# PostgreSQL 16 for the Almiras ERP, plus the backup tooling, all open source:
#   - age / age-keygen (BSD-3) to encrypt dumps, WAL segments and OpenBao snapshots to the owner's public key(s)
#   - supercronic (MIT) to run the backup schedule in the "backup" service
#   - bao CLI (MPL-2.0, from the pinned OpenBao image) for raft snapshots
# The same image runs the "db" service (official entrypoint, unchanged) and the "backup" service.
# Build from the repository root:  docker build -f deploy/postgres/Dockerfile -t almiras-postgres:16 .

ARG POSTGRES_IMAGE=postgres:16.15@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d
ARG GO_IMAGE=golang:1.26.9-trixie@sha256:f06b7122f888680048afb247c4e5f49c0069eb6c4c1ccae0f3f4834527a8d22c
ARG OPENBAO_IMAGE=openbao/openbao:2.7.1@sha256:6d2b93856e3fcf7b18ad855a0b51eaba474dc8b79cf554379ea32034797d2acf

FROM ${GO_IMAGE} AS tools
ARG AGE_VERSION=v1.2.1
ARG SUPERCRONIC_VERSION=v0.2.48
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=-trimpath
# hadolint ignore=DL3062  # versions are pinned through the build args above (@${..._VERSION})
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    go install -ldflags="-s -w" "filippo.io/age/cmd/age@${AGE_VERSION}" "filippo.io/age/cmd/age-keygen@${AGE_VERSION}" \
    && go install -tags timetzdata -ldflags="-s -w" "github.com/aptible/supercronic@${SUPERCRONIC_VERSION}"

FROM ${OPENBAO_IMAGE} AS openbao

FROM ${POSTGRES_IMAGE}
ENV TZ=Asia/Riyadh
COPY --from=tools /go/bin/age /go/bin/age-keygen /go/bin/supercronic /usr/local/bin/
COPY --from=openbao /usr/bin/bao /usr/local/bin/bao
COPY deploy/postgres/initdb/ /docker-entrypoint-initdb.d/
COPY deploy/postgres/sql/ /almiras/sql/
COPY deploy/postgres/bin/ /almiras/bin/
COPY deploy/backup/ /almiras/backup/
# /metrics: the shared textfile-metrics volume (same mode as in the application image).
RUN chmod 0755 /docker-entrypoint-initdb.d/*.sh /almiras/bin/* /almiras/backup/*.sh \
    && install -d -m 1777 /metrics \
    && age --version > /dev/null && bao version > /dev/null
