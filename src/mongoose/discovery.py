from __future__ import annotations

import socket
from typing import Any

# mDNS / DNS-SD advertisement so phones/tablets can DISCOVER the Mac on the LAN without
# anyone typing an IP. Android's Network Service Discovery (NSD) looks for exactly this
# shape: a _mongoose._tcp.local service. Soft dependency on `zeroconf`: if it isn't
# installed (or the network blocks mDNS), discovery just falls back to the QR's embedded
# host / manual entry, and the server runs normally. We NEVER let advertising failure
# break serving.

SERVICE_TYPE = "_mongoose._tcp.local."


def _primary_ip() -> str | None:
    """Best-effort LAN IP of this machine (the address phones should reach)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # doesn't actually send; just selects the outbound interface
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


class Advertiser:
    """Holds the zeroconf handle so it can be unregistered on shutdown."""

    def __init__(self) -> None:
        self._zc: Any = None
        self._info: Any = None

    def start(self, port: int, scheme: str = "http") -> bool:
        try:
            from zeroconf import Zeroconf, ServiceInfo
        except Exception:
            return False
        ip = _primary_ip()
        if not ip:
            return False
        try:
            hostname = socket.gethostname()
            name = f"Mongoose on {hostname}.{SERVICE_TYPE}"
            info = ServiceInfo(
                SERVICE_TYPE,
                name,
                addresses=[socket.inet_aton(ip)],
                port=port,
                properties={
                    b"scheme": scheme.encode(),
                    b"path": b"/pair",
                    b"service": b"mongoose",
                },
                server=f"{hostname}.local.",
            )
            zc = Zeroconf()
            zc.register_service(info)
            self._zc = zc
            self._info = info
            return True
        except Exception:
            # any failure (port in use, mDNS blocked) is non-fatal
            try:
                if self._zc:
                    self._zc.close()
            except Exception:
                pass
            self._zc = None
            return False

    def stop(self) -> None:
        try:
            if self._zc and self._info:
                self._zc.unregister_service(self._info)
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        finally:
            self._zc = None
            self._info = None


def advertise(port: int, scheme: str = "http") -> Advertiser | None:
    """Start advertising; return the Advertiser (so the caller can stop it) or None if
    discovery couldn't be started. Prints a short, honest line either way."""
    adv = Advertiser()
    ok = adv.start(port, scheme)
    if ok:
        ip = _primary_ip()
        print(f"Discovery: advertising as {SERVICE_TYPE} on the LAN "
              f"(phones can find this Mac automatically; manual address {scheme}://{ip}:{port}).")
        return adv
    print("Discovery: mDNS advertising unavailable (install 'zeroconf' to enable "
          "auto-discovery; pairing by QR/manual address still works).")
    return None
