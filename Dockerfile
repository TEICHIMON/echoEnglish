# Echo Loop web UI image.
FROM python:3.12-slim

# ffmpeg is required by pydub for m4a export; ca-certificates for TLS to the
# Google / OpenAI / edge endpoints; rclone for the optional Google Drive sync
# (ECHO_SYNC_METHOD=rclone) of generated audio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ECHO_OUTPUT_DIR=/data/outputs \
    BIND_ADDR=0.0.0.0 \
    PORT=8080

COPY requirements-web.txt ./
RUN pip install -r requirements-web.txt

# Copy the application (see .dockerignore for what is excluded).
COPY . .

RUN mkdir -p /data/outputs

EXPOSE 8080

# `sh -c` expands ${BIND_ADDR}/${PORT}; `exec` replaces the shell so uvicorn
# becomes PID 1 and receives SIGTERM for graceful container stops.
CMD ["sh", "-c", "exec uvicorn webapp.server:app --host ${BIND_ADDR} --port ${PORT}"]
