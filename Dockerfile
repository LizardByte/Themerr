# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:0.13 AS connector-uv

FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:11.0 AS connector
COPY --from=connector-uv /uv /usr/local/bin/uv
WORKDIR /connector
COPY connectors/jellyfin/ connectors/jellyfin/
COPY connectors/Directory.Build.props connectors/Directory.Build.props
COPY .editorconfig .editorconfig
COPY scripts/build_connector.py scripts/build_connector.py
COPY src/jellyfin/compatibility.py src/jellyfin/compatibility.props src/jellyfin/
COPY src/common/version.py src/common/version.py
ARG BUILD_VERSION
ARG THEMERR_VERSION=${BUILD_VERSION}
RUN uv run --no-project --python 3.14 scripts/build_connector.py

FROM denoland/deno:alpine-2.9.7 AS deno

FROM python:3.15-alpine3.24 AS build

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

# install build dependencies
RUN apk add --no-cache binutils npm

# Install locked dependencies into an isolated build environment.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV THEMERR_DOCKER=True

# setup app directory
WORKDIR /build
COPY . .
COPY --from=connector /connector/jellyfin-connector/ /build/jellyfin-connector/
COPY --from=connector /connector/src/common/version.py /build/src/common/version.py

# setup locked Python dependencies
RUN uv sync --locked --extra build --no-build --no-install-project --no-python-downloads --python /usr/local/bin/python

# setup npm and dependencies
RUN npm ci --ignore-scripts && npm run build

RUN THEMERR_PREBUILT_CONNECTOR=1 python scripts/build.py

FROM alpine:3.24 AS app

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

RUN apk add --no-cache ca-certificates libgcc libstdc++ tzdata

# Deno's Alpine image isolates its glibc libraries from Themerr's musl libraries.
COPY --from=deno /bin/deno /usr/local/bin/deno
COPY --from=deno /usr/local/lib/glibc/ /usr/local/lib/glibc/
COPY --from=deno /lib/ld-linux-* /lib/
COPY --from=deno /lib64/ /lib64/

# PyInstaller bundles Python, dependencies, web assets, locales, and connectors.
COPY --from=build /build/dist/themerr/ /opt/themerr/
COPY --from=build /build/LICENSE /app/LICENSE

# setup remaining env variables
ENV PATH="/opt/themerr:$PATH"
ENV THEMERR_DOCKER=True

# network setup
EXPOSE 9494
EXPOSE 9495

# setup user
ARG PGID=1000
ENV PGID=${PGID}
ARG PUID=1000
ENV PUID=${PUID}
ENV TZ="UTC"
ARG UNAME=lizard
ENV UNAME=${UNAME}

ENV HOME=/home/$UNAME

# setup user
RUN <<EOF
set -eu
if ! getent group "${PGID}" > /dev/null; then
    addgroup -g "${PGID}" "${UNAME}"
fi
group_name="$(getent group "${PGID}" | cut -d: -f1)"
adduser -D -h "${HOME}" -s /bin/sh -G "${group_name}" -u "${PUID}" "${UNAME}"
mkdir -p "${HOME}/.config/themerr"
ln -s "${HOME}/.config/themerr" /config
chown -R "${UNAME}" "${HOME}"
EOF

# mounts
VOLUME /config

USER ${UNAME}
WORKDIR ${HOME}

ENTRYPOINT ["themerr"]
HEALTHCHECK --start-period=90s CMD ["themerr", "--docker_healthcheck"]
