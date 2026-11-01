#!/bin/sh
set -eu
# Sentinel rewrites this file after elections, so generate a writable copy.
cat > /tmp/sentinel.conf <<'EOF'
port 26379
bind 0.0.0.0
protected-mode no
dir /tmp
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel monitor flight-cache redis-primary 6379 2
sentinel down-after-milliseconds flight-cache 2000
sentinel failover-timeout flight-cache 10000
sentinel parallel-syncs flight-cache 1
EOF
printf 'sentinel announce-ip %s\n' "$SENTINEL_HOSTNAME" >> /tmp/sentinel.conf
exec redis-server /tmp/sentinel.conf --sentinel
