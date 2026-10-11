FROM alpine:3.20
RUN apk add --no-cache python3 py3-pip && \
    pip install --no-cache-dir --break-system-packages dnspython==2.7.0
COPY authority.py /opt/routeros-authority/authority.py
EXPOSE 53/udp 53/tcp 8080/tcp
ENTRYPOINT ["python3", "/opt/routeros-authority/authority.py"]
