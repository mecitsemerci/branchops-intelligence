#!/usr/bin/env bash
# Build an offline wheelhouse for the air-gapped image build.
#
# Run on the bank's connected build host (or CI runner) that can reach the APPROVED INTERNAL artifact
# repository (Nexus/Artifactory PyPI proxy). The resulting ./wheelhouse directory is the only input the
# Dockerfile needs; the image build itself never accesses any package index.
#
#   PIP_INDEX_URL=https://artifactory.bank.internal/api/pypi/pypi-remote/simple ./scripts/build_wheelhouse.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${PIP_INDEX_URL:?set PIP_INDEX_URL to the internal PyPI mirror}"
PYTHON="${PYTHON:-python3.12}"
PLATFORM_ARGS=(--platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --python-version 3.12 --implementation cp --only-binary=:all:)

rm -rf wheelhouse && mkdir -p wheelhouse
# Runtime dependencies, pinned by the lock file, as Linux wheels for the target image.
"$PYTHON" -m pip download --dest wheelhouse --index-url "$PIP_INDEX_URL" "${PLATFORM_ARGS[@]}" -r requirements.lock
# Build backend + the application wheel itself.
"$PYTHON" -m pip download --dest wheelhouse --index-url "$PIP_INDEX_URL" --only-binary=:all: "hatchling>=1.25" pip setuptools wheel
"$PYTHON" -m pip wheel --no-deps --wheel-dir wheelhouse --index-url "$PIP_INDEX_URL" .
echo "wheelhouse ready: $(ls wheelhouse | wc -l) files"
