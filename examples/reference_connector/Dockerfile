ARG PYTHON_BASE=m.daocloud.io/docker.io/library/python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE}

WORKDIR /opt/reference-connector

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/reference-connector

RUN mkdir -p /var/lib/reference-connector \
    && chown 65532:65532 /var/lib/reference-connector

COPY --chown=65532:65532 reference_connector ./reference_connector
COPY --chown=65532:65532 contract-surface.json ./contract-surface.json

USER 65532:65532
EXPOSE 8090

CMD ["python", "-m", "reference_connector", "--config", "/etc/reference-connector/config.json", "serve"]
