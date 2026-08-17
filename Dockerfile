# Railway can build this with Nixpacks too, but an explicit Dockerfile keeps
# the Python version pinned and the image small and predictable.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change doesn't re-resolve the whole tree.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner/ ./scanner/
COPY api/ ./api/
COPY main.py ./

# Overridden by the Railway volume mount; here so local `docker run` works too.
ENV STORAGE_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

# Railway injects $PORT. Single worker on purpose: scans run in-process on
# background threads, and the crash-recovery sweep assumes one instance.
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 65"]
