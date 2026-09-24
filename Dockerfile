ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        avahi-daemon \
        avahi-utils \
        iproute2 \
        jq \
    && rm -rf /var/lib/apt/lists/*

# The Debian package ships an init-managed config and a dbus dependency we
# do not use; the daemon is configured from scratch at start-up instead.
RUN rm -f /etc/avahi/avahi-daemon.conf

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
