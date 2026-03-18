"""
slixmpp_fast — XEP-0484 FAST plugin for slixmpp.

Implements:
  - XEP-0484: Fast Authentication Streamlining Tokens
  - draft-schmaus-kitten-sasl-ht: Hashed Token (HT) SASL mechanism family

Depends on slixmpp_sasl2 (XEP-0388)

Usage:
    import slixmpp_fast

    # ...
    xmpp.register_plugin('xep_0484')

    # Load a previously saved token before calling connect():
    xmpp['xep_0484'].set_fast_token(token_str)

    # Handle new tokens issued by the server (save them for next time):
    @xmpp.add_event_handler('fast_token')
    async def on_fast_token(data):
        save(data['token'], data['expiry'])

    # Handle token expiry/invalidation (clear saved token, re-auth with password):
    @xmpp.add_event_handler('fast_token_expired')
    async def on_fast_token_expired(event):
        clear_saved_token()
"""

import os
import base64
import hashlib
import hmac
import json
import logging
import ssl
from datetime import datetime
from typing import ClassVar, Optional

from slixmpp.plugins import BasePlugin, register_plugin

from .slixmpp_sasl2 import SASL2Authenticate, SASL2Feature, SASL2Success, SASL2Failure

log = logging.getLogger(__name__)

NS_FAST = "urn:xmpp:fast:0"


# ---------------------------------------------------------------------------
# HT SASL mechanism helpers (draft-schmaus-kitten-sasl-ht)
# ---------------------------------------------------------------------------


def _channel_binding_data(xmpp, cb_type: str) -> bytes:
    """Extract channel binding data from the active TLS socket."""
    sock = xmpp.socket
    if not isinstance(sock, (ssl.SSLSocket, ssl.SSLObject)):
        return b""
    try:
        if cb_type == "tls-unique":
            return sock.get_channel_binding("tls-unique") or b""
        if cb_type == "tls-server-end-point":
            cert_der = sock.getpeercert(binary_form=True)
            return hashlib.sha256(cert_der).digest() if cert_der else b""
        if cb_type == "tls-exporter":
            if hasattr(sock, "export_keying_material"):
                return (
                    sock.export_keying_material(
                        "EXPORTER-Channel-Binding",
                        32,
                        context=b"",
                        use_context=True,
                    )
                    or b""
                )
    except Exception as exc:
        log.debug("channel binding (%s) unavailable: %s", cb_type, exc)
    return b""


def _cb_type_for_mech(mech_name: str) -> str:
    """Map an HT mechanism suffix to its TLS channel binding type string."""
    if mech_name.endswith("-ENDP"):
        return "tls-server-end-point"
    if mech_name.endswith("-EXPR"):
        return "tls-exporter"
    if mech_name.endswith("-UNIQ"):
        return "tls-unique"
    return ""  # -NONE


def _ht_client_msg(token_b64: str, authcid: str, cb_data: bytes) -> bytes:
    """
    Build the HT SASL initial client message.

    Wire format: authcid NUL HMAC-SHA-256(token, "Initiator" || cb_data)
    """
    token_bytes = base64.b64decode(token_b64)
    mac = hmac.new(token_bytes, b"Initiator" + cb_data, hashlib.sha256).digest()
    return authcid.encode("utf-8") + b"\x00" + mac


def _ht_verify_server(token_b64: str, server_data: bytes, cb_data: bytes) -> bool:
    """Verify the server's HT SASL response (constant-time comparison)."""
    token_bytes = base64.b64decode(token_b64)
    expected = hmac.new(token_bytes, b"Responder" + cb_data, hashlib.sha256).digest()
    return hmac.compare_digest(expected, server_data)


def _fast_mechanisms(feature: SASL2Feature) -> list:
    """Extract FAST mechanism names from a SASL2 server feature element."""
    inline = feature.get_inline()
    if inline is None:
        return []
    fast = inline.find("{%s}fast" % NS_FAST)
    if fast is None:
        return []
    return [m.text for m in fast.findall("{%s}mechanism" % NS_FAST)]


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class xep_0484(BasePlugin):
    """
    XEP-0484: Fast Authentication Streamlining Tokens.

    Hooks into XEP-0388 (SASL2) to:
      - Use an HT token mechanism instead of a password when a token is stored.
      - Request a fresh token from the server on every password-based login.
      - Discard expired/invalid tokens on authentication failure.

    Events fired:
      fast_token         {'token': str, 'expiry': str}  — new token received
      fast_token_expired {}                              — token rejected

    You should not need to listen to these events for normal operation.
    """

    name = "xep_0484"
    description = "XEP-0484: Fast Authentication Streamlining Tokens"
    # 'xep_0388' is already in the global registry (slixmpp_sasl2 registered it
    # at import time), so slixmpp can resolve and auto-enable it here.
    dependencies: ClassVar[set[str]] = {"xep_0388"}
    default_config = {
        # HT mechanisms tried in preference order (strongest channel binding first).
        "ht_mechanisms": [
            "HT-SHA-256-ENDP",
            "HT-SHA-256-EXPR",
            "HT-SHA-256-UNIQ",
            "HT-SHA-256-NONE",
        ],
        # Request a fresh token on every successful non-FAST login.
        "request_token": True,
        # storage path
        "storage": None,
    }

    def plugin_init(self):
        self.token: Optional[str] = None
        self.count: int = 0
        self._pending_cb_data: bytes = b""

        if self.storage:
            self.load()

        self.xmpp["xep_0388"].set_mechanism_override(self._maybe_use_fast)
        self.xmpp["xep_0388"].add_authenticate_hook(self._add_fast_elements)

        self.xmpp.add_event_handler("sasl2_success", self._handle_success)
        self.xmpp.add_event_handler("sasl2_failed", self._handle_failure)

    # def post_init(self):
    #     """Wire up SASL2 hooks after both plugins are fully initialised."""

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Storage
    # -----------------------------------------------------------------------
    def load(self):
        """Load XEP-0484 FAST token from XDG_STATE_HOME if available."""
        if not (self.storage and os.path.exists(self.storage)):
            log.warning("attempting to load missing/undefined token storage")
            return

        with open(self.storage) as fd:
            _token = json.load(fd)
            if "token" in _token:
                self.token = _token["token"]
                self.expiry = datetime.fromisoformat(_token(["expiry"]))
                self.count = _token["count"]

    def save(self):
        """
        Called when server provides a new FAST token or we use an existing one and bump it's usage count
        Save it to XDG_STATE_HOME for future logins.
        """
        os.makedirs(os.path.dirname(self.storage), exist_ok=True)
        log.debug("FAST: received new token (expiry=%s)", self.expiry)
        with open(self.storage, "w") as fd:
            json.dump(
                {
                    "token": self.token,
                    "expiry": self.expiry.isoformat(),
                    "count": self.counti,
                }
            )
            log.info(f"FAST token saved to {self._fast_token_file}")

        # notify listeners
        if self.token:
            self.xmpp.event(
                "fast_token",
                {"token": self.token, "expiry": self.expiry, "count": self.count},
            )
        else:
            self.xmpp.event("fast_token_expired")  # XXX a little

    def clear(self):
        """Discard the stored token (call after expiry or invalidation)."""
        self.token = None
        self.expiry = None
        self.count = None
        self.save()

    # -----------------------------------------------------------------------
    # SASL2 hooks
    # -----------------------------------------------------------------------

    def _pick_ht_mech(self, server_fast_mechs: list) -> Optional[str]:
        for mech in self.config.get("ht_mechanisms", []):
            if mech in server_fast_mechs:
                return mech
        return None

    def _maybe_use_fast(self, feature: SASL2Feature):
        """
        Mechanism-override hook: return (mech, initial_response) to use FAST,
        or None to fall through to standard SASL2 authentication.
        """
        if not self.token:
            return None

        server_fast_mechs = _fast_mechanisms(feature)
        mech = self._pick_ht_mech(server_fast_mechs)
        if not mech:
            log.debug(
                "FAST: token available but no supported HT mechanism "
                "in server list %s — falling back to standard auth",
                server_fast_mechs,
            )
            return None

        cb_type = _cb_type_for_mech(mech)
        cb_data = _channel_binding_data(self.xmpp, cb_type) if cb_type else b""
        authcid = self.xmpp.requested_jid.user

        self.count += 1
        # trigger update
        self.xmpp.event(
            "fast_token",
            {"token": self.token, "expiry": self.expiry, "count": self.count},
        )
        self._pending_cb_data = cb_data

        initial = _ht_client_msg(self.token, authcid, cb_data)
        log.debug("FAST: using HT mech=%s count=%d", mech, self.count)
        return (mech, initial)

    def _add_fast_elements(self, auth: SASL2Authenticate, feature: SASL2Feature):
        """
        Authenticate hook: add FAST-specific child elements to <authenticate>.

        - When using FAST (HT mechanism): add <fast count='N'/>.
        - When using a standard mechanism: add <request-token/> to get a token.
        """
        from xml.etree.ElementTree import SubElement

        server_fast_mechs = _fast_mechanisms(feature)
        if not server_fast_mechs:
            return  # server doesn't support FAST at all

        if self.token and auth.get_mechanism() in server_fast_mechs:
            # We're authenticating with an HT mechanism — add the replay counter.
            fast_el = SubElement(auth.xml, "{%s}fast" % NS_FAST)
            fast_el.set("count", str(self._fast_count))
        elif self.config.get("request_token", True):
            # Standard auth — request a token for future logins.
            preferred = self._pick_ht_mech(server_fast_mechs)
            if preferred:
                rt = SubElement(auth.xml, "{%s}request-token" % NS_FAST)
                rt.set("mechanism", preferred)
                log.debug("FAST: requesting token via %s", preferred)

    def _handle_success(self, stanza: SASL2Success):
        """Success hook: verify server HT response and extract any new token."""
        # Verify server's mutual-auth response if we used FAST.
        if self.token:
            server_data = stanza.get_additional_data()
            if server_data and not _ht_verify_server(
                self.token, server_data, self._pending_cb_data
            ):
                log.warning("FAST: server mutual authentication failed")

        # Collect any new token the server issued.
        token_el = stanza.xml.find("{%s}token" % NS_FAST)
        if token_el is not None:
            new_token = token_el.get("token")
            expiry = token_el.get("expiry")
            expiry = datetime.fromisoformat(expiry)  # TODO: catch errors
            if new_token:
                self.token = new_token
                self.expiry = expiry
                self.count = 0
                self.save()

    def _handle_failure(self, stanza: SASL2Failure):
        """Failure hook: discard the token if the server rejected it."""
        condition = stanza.get_condition()
        if self.fast_token and condition in ("credentials-expired", "not-authorized"):
            log.info("FAST: discarding invalid/expired token (condition=%s)", condition)
            self.clear()


# Register in slixmpp's global plugin registry at import time.
register_plugin(xep_0484)
