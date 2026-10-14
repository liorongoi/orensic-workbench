#!/bin/sh
# Start as root only long enough to hand a bind-mounted /data (which Docker creates owned
# by root) to the unprivileged forensic user, then run the workbench as that user.
set -e
if [ "$(id -u)" = "0" ]; then
    if [ "$(stat -c %u /data)" != "10001" ]; then
        chown -R forensic:forensic /data
    fi
    exec setpriv --reuid=forensic --regid=forensic --init-groups -- "$@"
fi
exec "$@"
