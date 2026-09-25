# Build as
# docker build -t fredhutch/motuz_app:latest -f deployment/docker/app/Dockerfile .

# The React frontend. uWSGI serves it (src/backend/api/views/frontend_views.py);
# Traefik in front of it only terminates TLS and routes.
FROM node:22-alpine AS frontend-build

WORKDIR /app
COPY ./package.json ./package-lock.json /app/
RUN npm ci --no-audit --no-fund

COPY ./src/frontend /app/src/frontend
RUN npm run build






FROM python:3.12-slim-bookworm AS build-image

ENV DEBIAN_FRONTEND=noninteractive

RUN set -x \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends --no-install-suggests \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/root/.local/bin:$PATH
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --user -r /code/requirements.txt






FROM python:3.12-slim-bookworm

COPY --from=build-image /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

ENV DEBIAN_FRONTEND=noninteractive

ARG RCLONE_VERSION=1.75.1
ARG RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
ARG RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9
ARG TARGETARCH=amd64

RUN set -x \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends --no-install-suggests \
        krb5-user \
        libpam-krb5 \
        sssd \
        libpam-sss \
        sudo \
        curl \
        ca-certificates \
        unzip \
    && if [ "$TARGETARCH" = "arm64" ]; then RCLONE_SHA256="$RCLONE_SHA256_ARM64"; else RCLONE_SHA256="$RCLONE_SHA256_AMD64"; fi \
    && RCLONE_NAME="rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}" \
    && curl -fLO "https://downloads.rclone.org/v${RCLONE_VERSION}/${RCLONE_NAME}.zip" \
    && echo "${RCLONE_SHA256}  ${RCLONE_NAME}.zip" | sha256sum -c - \
    && unzip "${RCLONE_NAME}.zip" \
    && cp "${RCLONE_NAME}/rclone" /usr/local/bin/ \
    && rm -rf "${RCLONE_NAME}.zip" "${RCLONE_NAME}/" \
    && apt-get remove --purge --auto-remove -y \
        unzip \
        curl \
    && apt-get purge -y --auto-remove \
    && rm -rf /var/lib/apt/lists/*

# Optional Azure CLI (az) for "Credentials from your home directory" with Azure CLI
# logins: rclone's azureblob use_az runs `az account get-access-token` as the user,
# with the user's ~/.azure (utils/local_credentials.py). Off by default because it
# adds about 590 MB to the image (azure-cli 2.90: 421 MB -> 1.01 GB). Build with
#   --build-arg INSTALL_AZURE_CLI=true [--build-arg AZURE_CLI_VERSION=2.90.0]
# From Microsoft's signed apt repository; the key fingerprint is checked.
ARG INSTALL_AZURE_CLI=false
ARG AZURE_CLI_VERSION=
ARG MICROSOFT_KEY_FINGERPRINT=BC528686B50D79E339D3721CEB3E94ADBE1229CF

RUN if [ "$INSTALL_AZURE_CLI" = "true" ]; then set -x \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends --no-install-suggests curl gpg ca-certificates \
    && install -d -m 755 /etc/apt/keyrings \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && gpg --show-keys --with-colons /etc/apt/keyrings/microsoft.gpg | grep -q "^fpr:::::::::${MICROSOFT_KEY_FINGERPRINT}:" \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bookworm main" \
        > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update -y \
    && apt-get install -y --no-install-recommends --no-install-suggests \
        "azure-cli${AZURE_CLI_VERSION:+=${AZURE_CLI_VERSION}-1~bookworm}" \
    && apt-get purge -y --auto-remove curl gpg \
    && rm -rf /var/lib/apt/lists/* \
    && AZURE_CONFIG_DIR=/tmp/az-build az version && rm -rf /tmp/az-build; \
    fi

ENV PYTHONUNBUFFERED=1
ENV DOCKER_CONTAINER=1
ENV PYTHON_ENVIRONMENT=prod
ENV MOTUZ_HOST=0.0.0.0
# libpython3.12.so.1.0 cannot be found if /etc is remapped
ENV LD_LIBRARY_PATH=/usr/local/lib

RUN set -x \
    && install -d -m 755 /root/.config/rclone/ \
    && touch /root/.config/rclone/rclone.conf

# FRONTEND_DIR (src/backend/api/config.py). The celery image inherits it unused (< 1 MB).
COPY --from=frontend-build /app/build /app/build

COPY ./src/backend /app/src/backend
COPY \
    ./deployment/docker/app/app-entrypoint.sh \
    ./deployment/docker/wait-for-it.sh \
    ./deployment/docker/load-secrets.sh \
    /app/src/backend/

WORKDIR /app/src/backend
CMD ["/app/src/backend/app-entrypoint.sh"]
