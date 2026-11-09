#!/bin/sh
# Forwards a Claude Code hook event (JSON on stdin) to Smitty's local endpoint.
# Usage in settings.json:  claude-hook.sh <event> [curl-timeout-seconds]
# Prints Smitty's JSON reply (a permission decision, or {}) on stdout.
# Always exits 0 and prints nothing if Smitty isn't running, so hooks are harmless.
EVENT="$1"
TIMEOUT="${2:-5}"
PORT="${SMITTY_PORT:-8765}"
curl -s -m "$TIMEOUT" -X POST -H 'Content-Type: application/json' \
     --data-binary @- "http://127.0.0.1:${PORT}/hook/${EVENT}" 2>/dev/null || true
exit 0
