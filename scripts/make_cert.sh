#!/usr/bin/env bash
# Generate a TLS cert+key for Mongoose so the cockpit can serve HTTPS on the LAN
# (which is what unlocks browser microphone capture on phones/tablets).
#
# Prefers mkcert (devices trust a local CA, no browser warnings). Falls back to a
# self-signed cert (zero install, but each device shows a one-time warning).
#
# Usage:
#   bash scripts/make_cert.sh                 # cert for localhost + this Mac's LAN IP
#   bash scripts/make_cert.sh mongoose.local  # also cover a hostname
#
# Then point Mongoose at the files and serve over https:
#   export MONGOOSE_TLS_CERTFILE=$PWD/certs/mongoose.pem
#   export MONGOOSE_TLS_KEYFILE=$PWD/certs/mongoose-key.pem
#   mongoose serve --host 0.0.0.0 --tls
set -euo pipefail

CERT_DIR="${MONGOOSE_CERT_DIR:-$PWD/certs}"
mkdir -p "$CERT_DIR"
CERT="$CERT_DIR/mongoose.pem"
KEY="$CERT_DIR/mongoose-key.pem"

# Discover this machine's primary LAN IP (best effort, macOS + Linux).
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
[ -z "${LAN_IP:-}" ] && LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[ -z "${LAN_IP:-}" ] && LAN_IP="127.0.0.1"

NAMES=("localhost" "127.0.0.1" "$LAN_IP")
for extra in "$@"; do NAMES+=("$extra"); done

echo "Generating cert for: ${NAMES[*]}"
echo "Output: $CERT_DIR"

if command -v mkcert >/dev/null 2>&1; then
  echo "Using mkcert (trusted local CA)."
  # Install the local CA into the system trust store (idempotent).
  mkcert -install
  mkcert -cert-file "$CERT" -key-file "$KEY" "${NAMES[@]}"
  echo
  echo "Done (mkcert). To trust this on your iPhone/iPad:"
  echo "  1. find the CA: mkcert -CAROOT  (copy rootCA.pem to the device)"
  echo "  2. install + trust the profile: Settings > General > VPN & Device Management,"
  echo "     then Settings > General > About > Certificate Trust Settings > enable it."
else
  echo "mkcert not found; falling back to a SELF-SIGNED cert (devices will warn once)."
  echo "For no warnings, install mkcert (brew install mkcert) and re-run."
  # Build a SAN list for openssl.
  SAN=""
  for n in "${NAMES[@]}"; do
    if [[ "$n" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then SAN+="IP:$n,"; else SAN+="DNS:$n,"; fi
  done
  SAN="${SAN%,}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=mongoose.local" \
    -addext "subjectAltName=$SAN"
  echo
  echo "Done (self-signed). Devices will show a one-time warning you can accept."
fi

chmod 600 "$KEY" 2>/dev/null || true
echo
echo "Now run:"
echo "  export MONGOOSE_TLS_CERTFILE=$CERT"
echo "  export MONGOOSE_TLS_KEYFILE=$KEY"
echo "  mongoose serve --host 0.0.0.0 --tls"
