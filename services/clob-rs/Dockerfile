# Multi-stage build for the clob-rs order-signing sidecar.
# Stage 1 compiles the Rust binary; stage 2 is a slim runtime.
FROM rust:1-slim AS build
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release

FROM debian:stable-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/target/release/clob-rs /usr/local/bin/clob-rs
EXPOSE 8082
CMD ["clob-rs"]
