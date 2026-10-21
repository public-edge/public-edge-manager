FROM python:3.15-alpine@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2

ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/public-edge/public-edge-manager" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir .
USER 65532:65532
EXPOSE 53/udp 53/tcp 853/tcp 8080/tcp
ENTRYPOINT ["python3", "-m", "public_edge_manager"]
