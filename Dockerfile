FROM rust:1.93-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY . .
RUN python3 -m venv /build-env && /build-env/bin/pip install 'maturin>=1.8,<2' && /build-env/bin/pip wheel --wheel-dir /wheels -r requirements-lock.txt && /build-env/bin/maturin build --release --locked --out /wheels

FROM python:3.11-slim-bookworm
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels 'trace-hash-sdk[all]==0.1.0' && rm -rf /wheels
RUN useradd --create-home --uid 10001 trace
USER trace
WORKDIR /work
ENTRYPOINT ["trace-hash"]
