# SENTRIX Portal image: python slim, Portal code only, run as the unprivileged
# sentrix user (start.py drops root after preparing the data volume).
# Build from the repository root (docker-compose.yml does):
#   docker build -f deploy/portal/Dockerfile -t sentrix-portal .
# Dockerfile.dockerignore (next to this file) limits the build context to the
# two packages, two tools and the requirements: .env, var/, models/, training/ and .venv/
# never reach the daemon.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 sentrix \
 && useradd --system --uid 10001 --gid sentrix --home-dir /app --shell /usr/sbin/nologin sentrix

WORKDIR /app
COPY deploy/portal/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY sentrix/ /app/sentrix/
COPY sentrix_portal/ /app/sentrix_portal/
# Company-side tools that run against the Portal's database (admin accounts,
# device provisioning) and the Postgres check.
COPY tools/provision_device.py tools/portal_pg_check.py /app/tools/
COPY deploy/portal/start.py /app/start.py

# Runtime files (key files when PORTAL_SECRET_KEY / PORTAL_DATA_KEY are not
# set, exports) live on the /data volume, never in the image.
RUN mkdir -p /data/portal /data/var && chown -R sentrix:sentrix /data
# The port: PORTAL_PORT (compose sets 8100), else a platform's PORT (Render,
# Railway), else 8100.
ENV SENTRIX_VAR_DIR=/data/var \
    PORTAL_DATA_DIR=/data/portal \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_TEST_MODE=1 \
    SENTRIX_TEST_MODE=1

# No USER line: Render and Railway mount the /data volume root-owned, so the
# container starts as root and start.py hands /data to sentrix, drops root
# (groups, gid, uid) and runs CMD as sentrix. The Portal never runs as root.
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; port = os.getenv('PORTAL_PORT') or os.getenv('PORT') or '8100'; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status == 200 else 1)"]

# One process, one worker: the device link hub keeps its sockets in memory.
ENTRYPOINT ["python", "/app/start.py"]
CMD ["python", "-m", "sentrix_portal"]
