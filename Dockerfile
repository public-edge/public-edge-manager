FROM python:3.12-alpine@sha256:236173eb74001afe2f60862de935b74fcbd00adfca247b2c27051a70a6a39a2d

ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/public-edge/public-edge-manager" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements-runtime.txt /app/
COPY src /app/src
RUN pip install --no-cache-dir --require-hashes --requirement requirements-runtime.txt
ENV PYTHONPATH=/app/src
USER 65532:65532
EXPOSE 53/udp 53/tcp 853/tcp 8080/tcp
ENTRYPOINT ["python3", "-m", "public_edge_manager"]
