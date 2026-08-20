#!/bin/sh
set -e

# No defaults here on purpose. This container binds all interfaces, so a
# fallback password is a published credential on every node that ever accepted
# it -- and the source password is what lets a caller PUSH audio into the
# stream the frontend plays as live radio. Refuse to start instead.
for var in ICECAST_SOURCE_PASSWORD ICECAST_ADMIN_PASSWORD; do
  eval "value=\${$var}"
  if [ -z "$value" ]; then
    echo "icecast: $var is not set." >&2
    echo "icecast: set it in the node's .env -- 'bash setup.sh' generates a random one," >&2
    echo "icecast: or run: openssl rand -base64 24" >&2
    exit 1
  fi
done

export ICECAST_SOURCE_PASSWORD ICECAST_ADMIN_PASSWORD

envsubst < /etc/icecast2/icecast.xml.template > /tmp/icecast.xml

exec gosu icecast icecast2 -c /tmp/icecast.xml
