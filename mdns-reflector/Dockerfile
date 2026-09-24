# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        avahi-daemon \
        avahi-utils \
        iproute2 \
        iptables \
    && rm -rf /var/lib/apt/lists/*

# reflect-filters is a Debian addition to Avahi, not upstream. Fail the build
# rather than ship an image that silently ignores the filter configuration.
RUN if ! grep -aq "reflect-filters" /usr/sbin/avahi-daemon; then \
        echo "ERROR: this Avahi build has no reflect-filters support" >&2; \
        exit 1; \
    fi

# The Debian package ships an init-managed config; ours is generated at start-up.
RUN rm -f /etc/avahi/avahi-daemon.conf

# Copy root filesystem
COPY rootfs /

LABEL \
    org.opencontainers.image.title="Home Assistant Add-on: mDNS Reflector" \
    org.opencontainers.image.description="Reflect mDNS/Bonjour between VLAN interfaces using Avahi" \
    org.opencontainers.image.licenses="MIT"
