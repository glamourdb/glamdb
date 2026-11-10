FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
RUN mkdir -p /app /data && chown 10001:10001 /app /data
COPY tools/alerting/transport.py /app/transport.py
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 9087
ENTRYPOINT ["python3", "/app/transport.py"]
