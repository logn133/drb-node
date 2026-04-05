#!/bin/sh
set -e

ICECAST_SOURCE_PASSWORD="${ICECAST_SOURCE_PASSWORD:-hackme}"
ICECAST_ADMIN_PASSWORD="${ICECAST_ADMIN_PASSWORD:-admin}"

export ICECAST_SOURCE_PASSWORD ICECAST_ADMIN_PASSWORD

envsubst < /etc/icecast2/icecast.xml.template > /tmp/icecast.xml

exec gosu icecast icecast2 -c /tmp/icecast.xml
