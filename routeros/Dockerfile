FROM python:3.12-alpine@sha256:236173eb74001afe2f60862de935b74fcbd00adfca247b2c27051a70a6a39a2d
COPY src/public_edge_manager /opt/public_edge_manager
COPY routeros/portable.py /opt/portable.py
ENV PYTHONPATH=/opt
EXPOSE 53/udp 53/tcp 8080/tcp
ENTRYPOINT ["python3", "/opt/portable.py"]
