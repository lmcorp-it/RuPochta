# RuPochta start page — production image
# Stage 1: build
FROM rust:1.97-slim AS builder
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY static ./static
RUN cargo build --release --locked

# Stage 2: runtime (distroless-style, minimal)
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/target/release/rupochta-web /app/rupochta-web
EXPOSE 8080
ENV RUPOCHTA_BIND=0.0.0.0:8080
USER nobody
ENTRYPOINT ["/app/rupochta-web"]
