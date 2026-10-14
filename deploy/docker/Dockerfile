# BranchOps Intelligence — single image, several commands (api | worker | dashboard | migrate | …).
#
# Two ways to obtain Python packages, selected with WHEELHOUSE_SOURCE:
#
#   offline (default, production / air-gapped)
#     Uses ./wheelhouse built by scripts/build_wheelhouse.sh from the internal PyPI mirror.
#     `pip install --no-index` guarantees that no package index is contacted during the build.
#       podman build -f deploy/docker/Dockerfile \
#         --build-arg BASE_IMAGE=registry.bank.internal/ubi9/python-312:latest \
#         -t registry.bank.internal/branchops/branchops-intelligence:0.1.0 .
#
#   online (developer laptops / CI with index access — used by docker compose and `make docker-build`)
#     Builds the wheels inside the image from PIP_INDEX_URL (default: public PyPI or your mirror).
#       docker build -f deploy/docker/Dockerfile --build-arg WHEELHOUSE_SOURCE=online \
#         --build-arg BASE_IMAGE=python:3.12-slim -t branchops-intelligence:dev .
#
# BuildKit only builds the stages that are needed, so an offline build never runs the online stage.

ARG BASE_IMAGE=registry.bank.internal/ubi9/python-312:latest
ARG WHEELHOUSE_SOURCE=offline

# --- package sources ----------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS wheelhouse-offline
USER 0
COPY wheelhouse/ /wheelhouse/

FROM ${BASE_IMAGE} AS wheelhouse-online
USER 0
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /src
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/pip \
    python3.12 -m pip wheel --index-url "${PIP_INDEX_URL}" --wheel-dir /wheelhouse -r requirements.lock \
 && python3.12 -m pip wheel --index-url "${PIP_INDEX_URL}" --wheel-dir /wheelhouse --no-deps .

FROM wheelhouse-${WHEELHOUSE_SOURCE} AS wheelhouse

# --- install (always offline from the wheelhouse) -----------------------------------------------
FROM ${BASE_IMAGE} AS build
USER 0
COPY --from=wheelhouse /wheelhouse/ /wheelhouse/
COPY requirements.lock /tmp/requirements.lock
RUN python3.12 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-index --find-links=/wheelhouse --no-cache-dir -r /tmp/requirements.lock \
 && /opt/venv/bin/pip install --no-index --find-links=/wheelhouse --no-cache-dir --no-deps branchops-intelligence \
 && /opt/venv/bin/pip check \
 && find /opt/venv -name '__pycache__' -prune -exec rm -rf {} +

# --- runtime ------------------------------------------------------------------------------------
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="branchops-intelligence" \
      org.opencontainers.image.description="On-premise agentic Jira incident quality analysis" \
      io.openshift.non-scalable="false"
USER 0
COPY --from=build /opt/venv /opt/venv
COPY templates/ /app/templates/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini
# Arbitrary-UID compatibility: OpenShift (and our compose file) run the container with a random UID in
# group 0, so everything the app may touch is owned by group 0 and group-writable.
RUN mkdir -p /app/config /tmp/nicegui \
 && chgrp -R 0 /app /tmp/nicegui && chmod -R g=u /app /tmp/nicegui
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    BRANCHOPS_HOME=/app \
    BRANCHOPS_CONFIG=/app/config/branchops.yaml \
    NICEGUI_STORAGE_PATH=/tmp/nicegui \
    MPLCONFIGDIR=/tmp
WORKDIR /app
USER 1001
EXPOSE 8080 8081 8090
ENTRYPOINT ["branchops"]
CMD ["api"]
