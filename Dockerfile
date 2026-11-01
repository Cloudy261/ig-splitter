# IG Splitter as a self-hosted service (see README.md).
FROM docker.io/library/python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

# jpegtran: lossless JPEG cuts. ImageMagick: lossless 16-bit PNGs.
# Debian's ImageMagick policy caps images at 16k px / 128 MP; panoramas can be bigger.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libjpeg-turbo-progs imagemagick \
 && rm -rf /var/lib/apt/lists/* \
 && for f in /etc/ImageMagick-*/policy.xml; do sed -i -E '/name="(width|height|area|memory|map|disk)"/d' "$f"; done

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py splitter.py auth.py ./
COPY static ./static

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app
USER 1000:1000
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IG_SPLITTER_LISTEN=0.0.0.0 \
    IG_SPLITTER_INPUT=/data/input \
    IG_SPLITTER_OUTPUT=/data/output

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"]
CMD ["python", "app.py", "--port", "8765", "--no-browser"]
