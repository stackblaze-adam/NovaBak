# ── Build nbdkit VDDK plugin (not in Debian slim repos) ─────────────────────
FROM debian:bookworm-slim AS nbdkit-vddk-build
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates git autoconf automake libtool pkg-config make gcc \
    nbdkit libnbd-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 https://gitlab.com/nbdkit/nbdkit.git \
    && cd nbdkit \
    && autoreconf -i \
    && ./configure --disable-dependency-tracking \
    && make -j"$(nproc)"

FROM python:3.11-slim

# Metadata
LABEL maintainer="THIS Cyber Security" \
      description="NovaBak — VM Backup Enterprise"

# Runtime: nbdkit + libnbd + VDDK plugin
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    nbdkit \
    libnbd0 \
    libnbd-bin \
    && rm -rf /var/lib/apt/lists/*

COPY --from=nbdkit-vddk-build /src/nbdkit/plugins/vddk/.libs/nbdkit-vddk-plugin.so \
    /usr/lib/x86_64-linux-gnu/nbdkit/plugins/nbdkit-vddk-plugin.so

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Vendor dir for VDDK tarball (mounted at runtime on production)
RUN mkdir -p data bin/ovftool static vendor/vddk

VOLUME ["/app/data"]

EXPOSE 8000

CMD ["python", "-u", "main.py"]
