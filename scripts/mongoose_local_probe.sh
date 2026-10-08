# Shared probe + browser URL helpers for scripts/launchers.
#
# Internal checks may use loopback, but user-facing URLs must not. Mongoose serves HTTPS by default,
# often with a self-signed first-run cert, so probes try HTTPS first and accept the local cert only for
# readiness checks. Browser/open URLs use localhost or the Mac's LAN IP, never 127.0.0.1 or 0.0.0.0.

: "${PORT:=${MONGOOSE_PORT:-8765}}"

_mongoose_env_value() {
  local key="$1" val=""
  if [[ -f .env ]]; then
    val="$(grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  fi
  # Shell env wins over .env, matching the process environment used by the launcher.
  eval "printf '%s' \"\${${key}:-$val}\""
}

_mongoose_tls_off() {
  local v
  v="$(_mongoose_env_value MONGOOSE_TLS | tr '[:upper:]' '[:lower:]' | xargs 2>/dev/null || true)"
  case "$v" in off|0|false|no|none) return 0 ;; esac
  return 1
}

mongoose_preferred_base() {
  if _mongoose_tls_off; then
    printf 'http://127.0.0.1:%s\n' "$PORT"
  else
    printf 'https://127.0.0.1:%s\n' "$PORT"
  fi
}

mongoose_base_candidates() {
  if _mongoose_tls_off; then
    printf 'http://127.0.0.1:%s\nhttps://127.0.0.1:%s\n' "$PORT" "$PORT"
  else
    printf 'https://127.0.0.1:%s\nhttp://127.0.0.1:%s\n' "$PORT" "$PORT"
  fi
}

mongoose_curl() {
  local path="$1"; shift || true
  local base
  for base in $(mongoose_base_candidates); do
    # -k is deliberate: the default first-run certificate is self-signed. This is a loopback-only
    # readiness probe, not a remote trust decision.
    if curl --connect-timeout 1 --max-time 3 -fsSk "$@" "${base}${path}" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

mongoose_probe_base() {
  local base
  for base in $(mongoose_base_candidates); do
    if curl --connect-timeout 1 --max-time 3 -fsSk "${base}/health" 2>/dev/null | grep -q '"name":"mongoose"'; then
      printf '%s\n' "$base"
      return 0
    fi
  done
  return 1
}

_mongoose_scheme_from_base() {
  case "$1" in
    https://*) printf 'https\n' ;;
    http://*) printf 'http\n' ;;
    *) if _mongoose_tls_off; then printf 'http\n'; else printf 'https\n'; fi ;;
  esac
}

mongoose_probe_scheme() {
  local base
  base="$(mongoose_probe_base 2>/dev/null || true)"
  if [[ -n "$base" ]]; then
    _mongoose_scheme_from_base "$base"
  else
    _mongoose_scheme_from_base "$(mongoose_preferred_base)"
  fi
}

mongoose_lan_ip() {
  # Prefer Python's routing view because it works across en0/en1/USB/Wi-Fi without guessing the device.
  local py="python3"
  [[ -x .venv/bin/python ]] && py=".venv/bin/python"
  "$py" - <<'PY' 2>/dev/null || true
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no payload is sent; it just chooses the outbound interface
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            print(ip)
    finally:
        s.close()
except Exception:
    pass
PY
}

_mongoose_lan_enabled() {
  local host allow
  host="$(_mongoose_env_value MONGOOSE_HOST | tr '[:upper:]' '[:lower:]' | xargs 2>/dev/null || true)"
  allow="$(_mongoose_env_value MONGOOSE_ALLOW_REMOTE | tr '[:upper:]' '[:lower:]' | xargs 2>/dev/null || true)"
  [[ "$host" == "0.0.0.0" || "$host" == "::" || "$allow" == "true" || "$allow" == "1" || "$allow" == "yes" || "$allow" == "on" ]]
}

mongoose_human_base() {
  local scheme ip
  scheme="$(mongoose_probe_scheme)"
  if _mongoose_lan_enabled; then
    ip="$(mongoose_lan_ip | head -n1)"
    if [[ -n "$ip" ]]; then
      printf '%s://%s:%s\n' "$scheme" "$ip" "$PORT"
      return 0
    fi
  fi
  # localhost is a human/browser URL for this Mac; unlike 127.0.0.1, it is not accidentally copied
  # into phone-facing instructions as if it would work off-device.
  printf '%s://localhost:%s\n' "$scheme" "$PORT"
}

mongoose_human_url() {
  local path="$1" base
  [[ "$path" == /* ]] || path="/$path"
  base="$(mongoose_human_base)"
  printf '%s%s\n' "$base" "$path"
}

mongoose_open_path() {
  local path="$1" url
  url="$(mongoose_human_url "$path")"
  open "$url" >/dev/null 2>&1 || true
  printf '%s\n' "$url"
}

# Owner-LOCAL URLs. The first-run / install / cockpit owner pages must open on THIS Mac's loopback,
# NOT the LAN IP: the owner-pairing secret and the cockpit are loopback-only, so a LAN URL lands on
# a gated or privacy-coarsened (and therefore dead) page. The phone-facing QR/link inside those
# pages still points at the LAN/DDNS address, only the page the OWNER opens on this Mac is forced
# to loopback. Uses the literal 127.0.0.1, not "localhost": on a machine where the server is not
# (yet) dual-bound to both loopback families, a browser trying "localhost" can try IPv6 first and
# hang waiting on a socket nothing is listening on, which is exactly what the updater's own
# browser-open of /install was observed doing.
mongoose_local_base() {
  local scheme
  scheme="$(mongoose_probe_scheme)"
  printf '%s://127.0.0.1:%s\n' "$scheme" "$PORT"
}

mongoose_local_url() {
  local path="$1"
  [[ "$path" == /* ]] || path="/$path"
  printf '%s%s\n' "$(mongoose_local_base)" "$path"
}

mongoose_open_local() {
  local path="$1" url
  url="$(mongoose_local_url "$path")"
  open "$url" >/dev/null 2>&1 || true
  printf '%s\n' "$url"
}

mongoose_page_ready() {
  # Verify the actual web page route responds, not just /health. Optional second arg is a grep pattern.
  local path="$1" pattern="${2:-}"
  local body
  body="$(mongoose_curl "$path" || true)"
  [[ -n "$body" ]] || return 1
  if [[ -n "$pattern" ]]; then
    printf '%s' "$body" | grep -Eiq "$pattern"
  else
    return 0
  fi
}
