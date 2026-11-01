# Eggdrop built from the official sources (github.com/eggheads/eggdrop)
FROM debian:bookworm-slim AS build
ARG EGGDROP_VERSION=1.10.2
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl tcl-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL "https://github.com/eggheads/eggdrop/archive/refs/tags/v${EGGDROP_VERSION}.tar.gz" -o eggdrop.tgz \
    && tar xzf eggdrop.tgz && cd eggdrop-${EGGDROP_VERSION} \
    && ./configure && make config && make && make install DEST=/opt/eggdrop

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        tcl libssl3 ca-certificates gettext-base tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 eggdrop
COPY --from=build /opt/eggdrop /opt/eggdrop
COPY scripts/cs2intel.tcl /opt/eggdrop/scripts/cs2intel.tcl
COPY config/eggdrop.conf.template /opt/eggdrop/eggdrop.conf.template
COPY config/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /opt/eggdrop/data /opt/eggdrop/logs \
    && chown -R eggdrop:eggdrop /opt/eggdrop
USER eggdrop
WORKDIR /opt/eggdrop
VOLUME ["/opt/eggdrop/data", "/opt/eggdrop/logs"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
