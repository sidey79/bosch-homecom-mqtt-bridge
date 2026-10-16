FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target /deps -r requirements.txt
RUN mkdir -p /out/data && chown 65532:65532 /out/data

FROM gcr.io/distroless/python3-debian13:nonroot@sha256:83aa8d4f74a4d7f7cf2d472054139bef71a927b76c680c0f2e1021d6b1d6d732
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/deps:/app/src
COPY --from=build /deps /deps
COPY VERSION /app/VERSION
COPY src /app/src
COPY --from=build --chown=nonroot:nonroot /out/data /data
VOLUME ["/data"]
USER nonroot:nonroot
ENTRYPOINT ["python3", "-m", "bosch_homecom_mqtt_bridge"]
