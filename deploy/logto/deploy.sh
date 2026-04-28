#!/usr/bin/env bash
# Deploy self-hosted Logto to your prod server.
#
# Idempotent: run it any time to sync the current docker-compose files to
# /opt/apps/logto-docker/ and `up -d` the stack. Existing data in the
# logto-pg-data volume is preserved across deploys.
#
# Required env (no defaults — every operator runs their own server):
#   LOGTO_SERVER     ssh target, e.g. root@1.2.3.4
#   LOGTO_LIVE_URL   public URL, e.g. https://auth.example.com
#   LOGTO_SSH_KEY    path to ssh private key (default: ~/.ssh/id_rsa)
#
# Prereqs on server (/opt/apps/logto-docker/), first run only:
#   - docker + docker compose v2
#   - .env file with LOGTO_DOMAIN, LOGTO_ENDPOINT, LOGTO_ADMIN_ENDPOINT,
#     POSTGRES_PASSWORD set (see .env.example; generated below if missing)
#   - host nginx vhost pointed at 127.0.0.1:3011 / 127.0.0.1:3012
#     (render nginx-auth.conf.template with your domain)
#   - DNS A record: <your-domain> -> this server's public IP

set -euo pipefail

: "${LOGTO_SERVER:?LOGTO_SERVER required, e.g. root@1.2.3.4}"
: "${LOGTO_LIVE_URL:?LOGTO_LIVE_URL required, e.g. https://auth.example.com}"
SERVER="$LOGTO_SERVER"
SSH_KEY="${LOGTO_SSH_KEY:-$HOME/.ssh/id_rsa}"
REMOTE_DIR="/opt/apps/logto-docker"
LIVE_URL="$LOGTO_LIVE_URL"

cd "$(dirname "$0")"

echo "==> Syncing compose files to $SERVER:$REMOTE_DIR"
ssh -i "$SSH_KEY" "$SERVER" "mkdir -p $REMOTE_DIR"
scp -i "$SSH_KEY" \
    docker-compose.yml \
    docker-compose.prod.yml \
    "$SERVER:$REMOTE_DIR/"

echo "==> Ensuring .env exists on server (only generated on first deploy)"
DOMAIN_FROM_URL="${LIVE_URL#https://}"
DOMAIN_FROM_URL="${DOMAIN_FROM_URL#http://}"
DOMAIN_FROM_URL="${DOMAIN_FROM_URL%%/*}"
ssh -i "$SSH_KEY" "$SERVER" "
  cd $REMOTE_DIR
  if [ ! -f .env ]; then
    PG=\$(openssl rand -hex 32)
    cat > .env <<EOF
LOGTO_DOMAIN=$DOMAIN_FROM_URL
LOGTO_ENDPOINT=$LIVE_URL
LOGTO_ADMIN_ENDPOINT=$LIVE_URL:8443
POSTGRES_PASSWORD=\$PG
EOF
    chmod 600 .env
    echo '  generated new .env — first deploy'
  else
    echo '  .env present, reusing'
  fi
"

echo "==> Pulling images + starting stack"
ssh -i "$SSH_KEY" "$SERVER" "
  cd $REMOTE_DIR
  docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
  docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
"

echo "==> Waiting for Logto to become reachable"
ssh -i "$SSH_KEY" "$SERVER" "
  for i in \$(seq 1 30); do
    if curl -sf http://127.0.0.1:3011/oidc/jwks >/dev/null; then
      echo '  core up'
      break
    fi
    sleep 2
  done
  curl -sf http://127.0.0.1:3011/oidc/jwks >/dev/null || { echo 'FAIL: core not ready'; exit 1; }

  # Public URL should return the OIDC discovery doc with TLS.
  if ! curl -sf $LIVE_URL/oidc/.well-known/openid-configuration | grep -q '\"issuer\":\"$LIVE_URL/oidc\"'; then
    echo 'WARN: public URL not serving correct issuer yet — check nginx + DNS + TLS'
    exit 1
  fi
  echo 'Public URL OK'
"

echo "==> Deploy complete: $LIVE_URL"
echo "    Admin console: $LIVE_URL/admin"
