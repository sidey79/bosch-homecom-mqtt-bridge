FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target /deps -r requirements.txt
RUN mkdir -p /out/data && chown 65532:65532 /out/data

FROM gcr.io/distroless/python3-debian13:nonroot@sha256:774595d652a294b54c9bd575b2d9fdd1a4b47547dc17b8bfa4c0e953c64855b3
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/deps:/app/src
COPY --from=build /deps /deps
COPY VERSION /app/VERSION
COPY src /app/src
COPY --from=build --chown=nonroot:nonroot /out/data /data
VOLUME ["/data"]
USER nonroot:nonroot
ENTRYPOINT ["python3", "-m", "bosch_homecom_mqtt_bridge"]
