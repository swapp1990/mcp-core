#!/usr/bin/env bash
# Provision Amazon SES for Logto's email connector.
#
# Idempotent-ish: reruns that hit existing identities/users will no-op with
# a non-zero exit; read the output and delete the old resource first if so.
#
# Usage:
#   AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=us-west-2 \
#     ./provision-ses.sh swapp1990.org logto-smtp
#
# Produces (to stdout, for pasting into Logto's SMTP connector config):
#   DKIM CNAME records to add to your DNS
#   SMTP host / port / username / password for Logto

set -euo pipefail

DOMAIN="${1:-swapp1990.org}"
IAM_USER="${2:-logto-smtp}"
REGION="${AWS_DEFAULT_REGION:-us-west-2}"

echo "==> Creating SES domain identity ($DOMAIN)..."
DKIM_JSON=$(aws sesv2 create-email-identity --email-identity "$DOMAIN" 2>&1 || true)
if echo "$DKIM_JSON" | grep -q "AlreadyExistsException"; then
  echo "  (identity already exists — fetching existing DKIM tokens)"
  DKIM_JSON=$(aws sesv2 get-email-identity --email-identity "$DOMAIN")
fi
echo "$DKIM_JSON" | py -c '
import json, sys
d = json.load(sys.stdin)
tokens = d.get("DkimAttributes", {}).get("Tokens") or []
for t in tokens:
    print(f"  {t}._domainkey.DOMAIN. CNAME {t}.dkim.amazonses.com.".replace("DOMAIN", "'"$DOMAIN"'"))
'

echo
echo "==> Creating IAM user ($IAM_USER) with SES-send-only policy..."
aws iam create-user --user-name "$IAM_USER" >/dev/null 2>&1 || echo "  (user exists, reusing)"
aws iam put-user-policy --user-name "$IAM_USER" --policy-name SendEmail --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}]
}'

echo "==> Creating access key..."
KEY_JSON=$(aws iam create-access-key --user-name "$IAM_USER")
ACCESS_KEY_ID=$(echo "$KEY_JSON" | py -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')
SECRET_ACCESS_KEY=$(echo "$KEY_JSON" | py -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')

echo
echo "==> Deriving SES SMTP password (SigV4)..."
SMTP_PASSWORD=$(py -c "
import hmac, hashlib, base64
secret = '$SECRET_ACCESS_KEY'
sig = hmac.new(('AWS4' + secret).encode(), b'11111111', hashlib.sha256).digest()
for s in ('$REGION', 'ses', 'aws4_request', 'SendRawEmail'):
    sig = hmac.new(sig, s.encode(), hashlib.sha256).digest()
print(base64.b64encode(bytes([4]) + sig).decode())
")

cat <<EOF

==== Logto SMTP connector config ====
Host:     email-smtp.$REGION.amazonaws.com
Port:     587 (STARTTLS) or 465 (implicit TLS)
Username: $ACCESS_KEY_ID
Password: $SMTP_PASSWORD
From:     no-reply@$DOMAIN  (only works after DKIM verification)

==== Before sending works ====
1. Add the DKIM CNAME records above to your DNS; propagation ~10 min.
2. SES is currently in sandbox — only sends to verified recipients.
   For real users you'll need to request production access:
     https://console.aws.amazon.com/ses/home?region=$REGION#/account
3. While sandboxed, verify swapp19902@gmail.com or equivalent recipient(s):
     aws sesv2 create-email-identity --email-identity you@example.com
EOF
