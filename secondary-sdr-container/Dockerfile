# Secondary-SDR Container — node-26#9
#
# Claims the node's SECOND physical SDR (the first is always op25's). Mode is
# chosen at runtime via the control API, not baked in: dump1090 for ADS-B,
# AIS-catcher for AIS (op25_2 mode is not handled here yet — see node-26#9).
#
# Device claiming is by RTL-SDR index, not serial (op25's DeviceConfig.args
# has no serial concept either — see op25-container/app/models.py). Index 0
# is reserved for op25; this container always addresses index 1. That's a
# real limitation once serial-stable device binding matters (hot-unplug /
# replug can swap indices) — tracked in node-26#9, not fixed here.
#
# UNVERIFIED: this image has not been built or run against real hardware in
# this session (sandboxed authoring machine, no docker). dump1090 and
# AIS-catcher's exact CLI flags below are believed correct from their
# published docs but not confirmed against a real capture — the CTO/QA
# review before this ships to a real node should build and smoke-test it.
FROM python:3.14-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        git build-essential cmake pkg-config \
        librtlsdr-dev libusb-1.0-0-dev libssl-dev zlib1g-dev libzstd-dev libncurses-dev usbutils

# readsb (wiedehopf) — ADS-B decoder. antirez/dump1090 was used here first but
# has no --write-json at all (it only serves /data.json over --net), so the
# decoder exited on an unknown flag. readsb writes the dump1090-fa style
# aircraft.json that _read_adsb_snapshot() parses.
RUN git clone --depth 1 https://github.com/wiedehopf/readsb /opt/readsb && \
    cd /opt/readsb && make RTLSDR=yes

# AIS-catcher — AIS decoder.
RUN git clone https://github.com/jvde-github/AIS-catcher /opt/AIS-catcher && \
    cd /opt/AIS-catcher && mkdir build && cd build && cmake .. && make

EXPOSE 8002

VOLUME ["/configs"]

WORKDIR /app
COPY ./app /app

COPY docker-entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "main.py"]
