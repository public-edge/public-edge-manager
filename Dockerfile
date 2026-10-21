FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4

ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/public-edge/public-edge-manager" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements-runtime.lock /app/
COPY src /app/src
RUN pip install --no-cache-dir --require-hashes --requirement requirements-runtime.lock
ENV PYTHONPATH=/app/src
USER 65532:65532
EXPOSE 53/udp 53/tcp 853/tcp 8080/tcp
ENTRYPOINT ["python3", "-m", "public_edge_manager"]
