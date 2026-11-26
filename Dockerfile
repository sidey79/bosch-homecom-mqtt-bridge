FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50 AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target /deps -r requirements.txt
RUN mkdir -p /out/data && chown 65532:65532 /out/data

FROM gcr.io/distroless/python3-debian13:nonroot@sha256:72de1dda9d4c52c370e2ef2d93c28e3e1a8193c92058b7dfa42bace38a83d114
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/deps:/app/src
COPY --from=build /deps /deps
COPY VERSION /app/VERSION
COPY src /app/src
COPY --from=build --chown=nonroot:nonroot /out/data /data
VOLUME ["/data"]
USER nonroot:nonroot
ENTRYPOINT ["python3", "-m", "bosch_homecom_mqtt_bridge"]
