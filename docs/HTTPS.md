# HTTPS for the cockpit (unlocks voice on phones/tablets)

Browser microphone capture AND web-push notifications need a "secure context" (https or
localhost). So to use voice or get push notifications from a phone over the LAN, the cockpit
must serve https.

## Easiest: one click from the cockpit (no command line)

1. Open the cockpit on the Mac, go to the **System** tab.
2. Under **HTTPS**, click **Set up HTTPS (one click)**. This generates a self-signed certificate
   in-process (no openssl/mkcert/certbot needed) covering localhost, this Mac's LAN IP, and
   mongoose.local, and points Mongoose at it.
3. Restart Mongoose with HTTPS:
   ```
   mongoose serve --host 0.0.0.0 --tls
   ```
   (When the cert env vars are set, https is auto-on even without --tls.)
4. On each phone, open `https://<mac-lan-ip>:8765` and accept the one-time security warning
   (it's self-signed, so the browser asks once). Voice and notifications then work.

That's the whole flow for friends-on-your-wifi. The warning is the only friction; to remove it
entirely, use mkcert or a real domain (below).

---

## Option A: built-in TLS (one process, recommended for daily use)

1. Generate a cert. mkcert is preferred (devices trust it, no warnings):

   ```
   brew install mkcert          # once
   bash scripts/make_cert.sh    # cert for localhost + this Mac's LAN IP
   ```

   No mkcert? The script falls back to a self-signed cert (devices warn once).

2. Point Mongoose at the cert and serve https:

   ```
   export MONGOOSE_TLS_CERTFILE=$PWD/certs/mongoose.pem
   export MONGOOSE_TLS_KEYFILE=$PWD/certs/mongoose-key.pem
   mongoose serve --host 0.0.0.0 --tls
   ```

   (When both env vars are set, https is auto-on even without --tls.)

3. Trust the mkcert CA on each device ONCE, so there are no warnings and the mic works:
   - Find the CA: `mkcert -CAROOT` (copy `rootCA.pem` to the device).
   - iOS: install the profile (Settings > General > VPN & Device Management), then
     enable it under Settings > General > About > Certificate Trust Settings.

   This per-device CA trust is unavoidable for ANY LAN https without warnings, it's how
   a browser decides to trust a private cert.

Then on each device open `https://<mac-lan-ip>:8765/login`, enter the token once, and
voice works.

## Option B: Caddy reverse proxy (automatic certs)

If you'd rather not manage certs by hand, run Caddy in front. It issues and renews a
local cert automatically:

```
brew install caddy
caddy run --config scripts/Caddyfile
caddy trust         # install Caddy's local CA (then trust it on devices as above)
```

Mongoose stays on plain http on localhost; Caddy terminates TLS and proxies to it.
Mongoose's own auth (token/cookie + cockpit nonce) still applies behind the proxy.

## Checking it

`mongoose doctor` reports an `https` line: configured (cert+key present) or not. When
not configured, the cockpit serves http and the voice tab tells LAN devices that mic
capture needs https.

## If you don't want HTTPS

Everything except browser voice works over plain http after login. Voice still works on
the Mac itself (localhost is a secure context) and via the CLI voice command.
