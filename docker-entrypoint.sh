#!/bin/sh
# Fail loudly on a misconfiguration rather than starting an unsafe server.
set -e

fail() { echo "gmg: $1" >&2; exit 1; }

if [ "${EGX_ENV}" = "production" ]; then
    # Without this the session cookie is signed with a development default,
    # which would let anyone forge a session. The application also refuses,
    # but failing here gives a readable message instead of a stack trace.
    [ -n "${EGX_AUTH_SECRET}" ] || fail \
      "EGX_AUTH_SECRET is not set. Generate one with:
      python -c \"import secrets; print(secrets.token_urlsafe(48))\""

    if [ "${EGX_ALLOW_SYNTHETIC_DATA}" = "true" ]; then
        fail "EGX_ALLOW_SYNTHETIC_DATA=true in production. Generated demonstration
      data must never be stored on a production deployment."
    fi

    if [ "${EGX_COOKIE_SECURE}" != "true" ]; then
        echo "gmg: WARNING — EGX_COOKIE_SECURE is not true. The session cookie will" >&2
        echo "gmg: be sent over plain HTTP. Set it once TLS terminates in front." >&2
    fi
fi

# Create or migrate the schema before serving. Safe to run repeatedly.
if [ "${GMG_SKIP_INIT}" != "true" ]; then
    echo "gmg: initialising database schema"
    python scripts/init_db.py
fi

exec "$@"
