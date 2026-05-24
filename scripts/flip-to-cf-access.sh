#!/usr/bin/env bash
# Flip the running cockpit stack from AUTH_MODE=local to AUTH_MODE=cf_access.
#
# Usage:
#   flip-to-cf-access.sh <AUD>             # AUD from CF dashboard
#   flip-to-cf-access.sh --from-cloudflared # extract AUD from cloudflared JWT
#
# Where to find AUD:
#   one.dash.cloudflare.com → Zero Trust → Access → Applications →
#     <cockpit-spike> → Overview → "Application Audience (AUD) Tag"
#
# What it does:
#   1. Resolves AUD + TEAM_DOMAIN (from argument or cloudflared JWT).
#   2. Updates ~/dev/caio-cockpit/.env with CF_ACCESS_* + COCKPIT_WORKER_TOKEN.
#   3. Rebuilds + restarts backend & frontend containers.
#   4. Updates the launchd plist of the cockpit-decision-worker with the new
#      COCKPIT_WORKER_TOKEN and reloads it.
#   5. Prints a smoke test you can run from the iPhone.
#
# Idempotent: re-run safely.

set -euo pipefail

ENV_FILE="$HOME/dev/caio-cockpit/.env"
COMPOSE_DIR="$HOME/dev/caio-cockpit"
WORKER_PLIST="$HOME/Library/LaunchAgents/ai.openclaw.cockpit-decision-worker.plist"
ALLOWED_EMAILS="${CF_ACCESS_ALLOWED_EMAILS:-pedro.braga.2007@gmail.com}"
APP_URL="https://cockpit-spike.ocaio.app"
TEAM_DOMAIN_DEFAULT="falling-haze-5df4"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found" >&2
  exit 1
fi

# --- 1: resolve AUD + TEAM_DOMAIN --------------------------------------------

if [[ $# -lt 1 ]]; then
  cat >&2 <<USAGE
Usage:
  $0 <AUD>                  # paste AUD from CF Zero Trust dashboard
  $0 --from-cloudflared     # try to extract from cloudflared local token

Find AUD at:
  one.dash.cloudflare.com → Zero Trust → Access → Applications →
    cockpit-spike → Overview → "Application Audience (AUD) Tag"
USAGE
  exit 1
fi

decode_b64url() {
  local p="${1//-/+}"
  p="${p//_//}"
  local pad=$(( (4 - ${#p} % 4) % 4 ))
  for ((i=0; i<pad; i++)); do p="${p}="; done
  printf '%s' "$p" | base64 -D 2>/dev/null || printf '%s' "$p" | base64 -d
}

if [[ "$1" == "--from-cloudflared" ]]; then
  echo "→ Reading cloudflared token (run 'cloudflared access login $APP_URL/' first if missing)…"
  TOKEN="$(cloudflared access token --app "$APP_URL/" 2>/dev/null | tr -d '\n')"
  if [[ -z "$TOKEN" ]]; then
    echo "ERROR: no cloudflared token. Run 'cloudflared access login $APP_URL/' and complete the email auth." >&2
    exit 2
  fi
  PAYLOAD_JSON="$(decode_b64url "$(printf '%s' "$TOKEN" | cut -d. -f2)")"
  AUD="$(printf '%s' "$PAYLOAD_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); a=d["aud"]; print(a[0] if isinstance(a,list) else a)')"
  TEAM_DOMAIN="$(printf '%s' "$PAYLOAD_JSON" | python3 -c 'import json,sys,urllib.parse as u; d=json.load(sys.stdin); h=u.urlparse(d["iss"]).hostname; print(h.split(".",1)[0])')"
else
  AUD="$1"
  TEAM_DOMAIN="${2:-$TEAM_DOMAIN_DEFAULT}"
fi

# Sanity: AUD is typically a 64-hex-char string
if ! [[ "$AUD" =~ ^[a-f0-9]{32,}$ ]]; then
  echo "WARN: AUD '$AUD' doesn't look like a CF Access audience tag (expected hex). Proceeding anyway." >&2
fi

if [[ -z "$AUD" || -z "$TEAM_DOMAIN" ]]; then
  echo "ERROR: empty AUD or TEAM_DOMAIN" >&2
  exit 3
fi
echo "  ✓ team_domain=$TEAM_DOMAIN"
echo "  ✓ aud=$AUD"

# --- 2: update .env -----------------------------------------------------------

# Generate worker token if not already set in .env
if ! grep -q '^COCKPIT_WORKER_TOKEN=.\{50,\}' "$ENV_FILE"; then
  WORKER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  echo "  ✓ generated COCKPIT_WORKER_TOKEN ($(echo -n "$WORKER_TOKEN" | wc -c | tr -d ' ') chars)"
else
  WORKER_TOKEN="$(grep '^COCKPIT_WORKER_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  echo "  ✓ keeping existing COCKPIT_WORKER_TOKEN from .env"
fi

# Backup .env once per day
cp -n "$ENV_FILE" "${ENV_FILE}.pre-cf-access-$(date +%Y%m%d)" 2>/dev/null || true

upsert_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # macOS sed -i requires '' as the in-place backup arg
    sed -i '' "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

upsert_env AUTH_MODE cf_access
upsert_env CF_ACCESS_TEAM_DOMAIN "$TEAM_DOMAIN"
upsert_env CF_ACCESS_AUDIENCE "$AUD"
upsert_env CF_ACCESS_ALLOWED_EMAILS "$ALLOWED_EMAILS"
upsert_env COCKPIT_WORKER_TOKEN "$WORKER_TOKEN"

echo "  ✓ .env updated (AUTH_MODE=cf_access, CF_ACCESS_*, COCKPIT_WORKER_TOKEN)"

# --- 3: rebuild + restart stack ----------------------------------------------

echo "→ Restarting backend + frontend containers…"
(
  cd "$COMPOSE_DIR"
  docker compose up -d --build backend frontend
) >/dev/null
echo "  ✓ containers up"

# --- 4: update worker plist + reload -----------------------------------------

if [[ -f "$WORKER_PLIST" ]]; then
  /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:COCKPIT_WORKER_TOKEN $WORKER_TOKEN" "$WORKER_PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:COCKPIT_WORKER_TOKEN string $WORKER_TOKEN" "$WORKER_PLIST"
  launchctl bootout "gui/$(id -u)" "$WORKER_PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$WORKER_PLIST"
  echo "  ✓ worker plist updated + reloaded"
else
  echo "  ⚠ worker plist not found at $WORKER_PLIST — skip worker reload"
fi

# --- 5: smoke ----------------------------------------------------------------

cat <<EOF

✅ Flip complete.

Smoke test:
  • iPhone Safari (cellular):  $APP_URL/caio
    Expect: CF Access magic-link prompt if cookie expired; then /caio loads
    WITHOUT any bearer-paste prompt. 3 tabs render normally.
  • Worker self-test:
      curl -s -X POST -H "X-Cockpit-Worker-Token: $WORKER_TOKEN" \\
        http://127.0.0.1:8001/api/v1/caio/think-loop/decisions/__nope__/start -i | head -2
    Expect 409 ('no decision exists') or 401 if config not picked up.
  • Backend logs:
      docker logs openclaw-mission-control-backend-1 --since 1m | grep -iE 'cf_access|auth'

To roll back:
  sed -i '' 's|^AUTH_MODE=cf_access|AUTH_MODE=local|' $ENV_FILE
  (cd $COMPOSE_DIR && docker compose up -d backend frontend)
EOF
