# Sandbox for generated transforms: Python + polars, nothing else. Build with `make sandbox-image`,
# which first writes requirements.txt (polars at the version in backend/uv.lock, hash-pinned) from
# the lockfile. Hardening that needs runtime flags (no network, read-only rootfs, dropped caps,
# limits) is applied by sluice.sandbox.runner when it starts the container.
FROM python:3.14.10-slim@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt
RUN PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
        pip install --no-deps --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && PIP_ROOT_USER_ACTION=ignore pip uninstall --yes pip \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin sandbox \
    && mkdir -p /app /work/in /work/out

COPY run_transform.py /app/run_transform.py
RUN chmod 0444 /app/run_transform.py && chmod 0555 /app

USER 10001:10001
WORKDIR /work
# -I: isolated mode (no PYTHON* env, no user site, script dir not on sys.path); -B: no .pyc.
ENTRYPOINT ["python", "-I", "-B", "/app/run_transform.py"]
