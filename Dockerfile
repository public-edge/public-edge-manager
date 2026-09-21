FROM python:3.12-alpine@sha256:c4634f578a412db396771b61b064c6e546c9d6414c7fb5b1b05d5871f1885f7b

ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/public-edge/public-edge-manager" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY src/public_edge_manager /app/public_edge_manager
USER 65532:65532
EXPOSE 53/udp 53/tcp 8080/tcp
ENTRYPOINT ["python3", "-m", "public_edge_manager"]
