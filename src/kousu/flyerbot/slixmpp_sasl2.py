"""
slixmpp_sasl2 — XEP-0388 Extensible SASL Profile (SASL2) plugin for slixmpp.

Handles the SASL2 stream feature negotiation.  Other plugins (e.g. XEP-0484
FAST) can hook into the exchange via the extension points below.

Extension points for inline-feature plugins
-------------------------------------------
Call these from ``post_init()`` after both plugins have been loaded:

    sasl2 = xmpp['xep_0388']

    sasl2.set_mechanism_override(fn)
        fn(server_features) → (mech_name: str, initial_response: bytes) | None
        Return non-None to replace the default mechanism selection entirely.
        Called before the <authenticate> stanza is built.

    sasl2.add_authenticate_hook(fn)
        fn(auth_stanza, server_features) → None
        Modify the <authenticate> stanza in-place (e.g. add <request-token>).
        Called after the mechanism and initial-response are set, before send.
"""

import base64
import logging
from typing import Callable, ClassVar, Optional
from xml.etree.ElementTree import SubElement

from slixmpp.plugins import BasePlugin, register_plugin
from slixmpp.util import sasl
from slixmpp.stanza import StreamFeatures
from slixmpp.xmlstream import ElementBase, StanzaBase, register_stanza_plugin
from slixmpp.xmlstream.matcher import MatchXPath
from slixmpp.xmlstream.handler import Callback

log = logging.getLogger(__name__)

NS_SASL2 = "urn:xmpp:sasl:2"


# ---------------------------------------------------------------------------
# Stanzas
# ---------------------------------------------------------------------------


class SASL2Feature(ElementBase):
    """
    Stream feature advertised by the server inside <stream:features>:

      <authentication xmlns='urn:xmpp:sasl:2'>
        <mechanism>SCRAM-SHA-256</mechanism>
        <inline>…</inline>
      </authentication>
    """

    namespace = NS_SASL2
    name = "authentication"
    plugin_attrib = "sasl2"
    interfaces = {"mechanisms"}

    def get_mechanisms(self) -> list:
        return [m.text for m in self.xml.findall("{%s}mechanism" % NS_SASL2)]

    def get_inline(self):
        """Return the raw <inline> element, or None."""
        return self.xml.find("{%s}inline" % NS_SASL2)


class SASL2Authenticate(StanzaBase):
    """
    Client authentication request:

      <authenticate xmlns='urn:xmpp:sasl:2' mechanism='…'>
        <initial-response>base64…</initial-response>
      </authenticate>

    Inline-feature plugins add their own child elements via authenticate hooks.
    """

    namespace = NS_SASL2
    name = "authenticate"
    plugin_attrib = "sasl2_authenticate"
    interfaces = set()

    def setup(self, xml=None):
        StanzaBase.setup(self, xml)
        self.xml.tag = self.tag_name()

    def get_mechanism(self) -> str:
        return self.xml.get("mechanism", "")

    def set_mechanism(self, value: str):
        self.xml.set("mechanism", value)

    def get_initial_response(self) -> bytes:
        ir = self.xml.find("{%s}initial-response" % NS_SASL2)
        if ir is not None and ir.text:
            return base64.b64decode(ir.text)
        return b""

    def set_initial_response(self, value: bytes):
        ir = self.xml.find("{%s}initial-response" % NS_SASL2)
        if ir is None:
            ir = SubElement(self.xml, "{%s}initial-response" % NS_SASL2)
        ir.text = base64.b64encode(value).decode("ascii") if value else "="


class SASL2Response(StanzaBase):
    """
    Client response during a multi-step SASL2 exchange:

      <response xmlns='urn:xmpp:sasl:2'>
        <additional-data>base64…</additional-data>
      </response>
    """

    namespace = NS_SASL2
    name = "response"
    plugin_attrib = "sasl2_response"
    interfaces = set()

    def setup(self, xml=None):
        StanzaBase.setup(self, xml)
        self.xml.tag = self.tag_name()

    def set_additional_data(self, value: bytes):
        ad = self.xml.find("{%s}additional-data" % NS_SASL2)
        if ad is None:
            ad = SubElement(self.xml, "{%s}additional-data" % NS_SASL2)
        ad.text = base64.b64encode(value).decode("ascii")


class SASL2Success(StanzaBase):
    """
    Server success response:

      <success xmlns='urn:xmpp:sasl:2'>
        <authorization-identity>user@example.com</authorization-identity>
        <additional-data>base64…</additional-data>
        <!-- inline feature responses go here, e.g. <token xmlns='urn:xmpp:fast:0'> -->
      </success>
    """

    namespace = NS_SASL2
    name = "success"
    plugin_attrib = "sasl2_success"
    interfaces = set()

    def setup(self, xml=None):
        StanzaBase.setup(self, xml)
        self.xml.tag = self.tag_name()

    def get_authorization_identity(self) -> str:
        ai = self.xml.find("{%s}authorization-identity" % NS_SASL2)
        return ai.text if ai is not None else ""

    def get_additional_data(self) -> bytes:
        ad = self.xml.find("{%s}additional-data" % NS_SASL2)
        if ad is not None and ad.text:
            return base64.b64decode(ad.text)
        return b""


class SASL2Failure(StanzaBase):
    """
    Server failure response:

      <failure xmlns='urn:xmpp:sasl:2'>
        <not-authorized/>
        <text>Human-readable reason</text>
      </failure>
    """

    namespace = NS_SASL2
    name = "failure"
    plugin_attrib = "sasl2_failure"
    interfaces = set()

    def setup(self, xml=None):
        StanzaBase.setup(self, xml)
        self.xml.tag = self.tag_name()

    def get_condition(self) -> str:
        for child in self.xml:
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag != "text":
                return tag
        return "unknown"

    def get_text(self) -> str:
        el = self.xml.find("{%s}text" % NS_SASL2)
        return el.text if el is not None else ""


class SASL2Continue(StanzaBase):
    """
    Server continue response during a multi-step exchange:

      <continue xmlns='urn:xmpp:sasl:2'>
        <additional-data>base64…</additional-data>
        <tasks><task>HOTP-TOTP</task></tasks>
      </continue>
    """

    namespace = NS_SASL2
    name = "continue"
    plugin_attrib = "sasl2_continue"
    interfaces = set()

    def setup(self, xml=None):
        StanzaBase.setup(self, xml)
        self.xml.tag = self.tag_name()

    def get_additional_data(self) -> bytes:
        ad = self.xml.find("{%s}additional-data" % NS_SASL2)
        if ad is not None and ad.text:
            return base64.b64decode(ad.text)
        return b""


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class xep_0388(BasePlugin):
    """
    XEP-0388: Extensible SASL Profile (SASL2).

    Fires the same events as slixmpp's built-in SASL1 handler:
      auth_success   — authentication succeeded (stanza = SASL2Success)
      failed_auth    — authentication failed    (stanza = SASL2Failure)
      failed_all_auth — no appropriate mechanism found
    """

    name = "xep_0388"
    description = "XEP-0388: Extensible SASL Profile (SASL2)"
    dependencies = set()
    default_config = {
        # Stream feature processing order — before SASL1 (order=100).
        "order": 90,
    }

    def plugin_init(self):
        self._mechanism_override: Optional[Callable] = None
        self._authenticate_hooks: list[Callable] = []
        self._pending_mech = None

        register_stanza_plugin(StreamFeatures, SASL2Feature)

        for cls in (SASL2Success, SASL2Failure, SASL2Continue):
            self.xmpp.register_stanza(cls)

        self.xmpp.register_handler(
            Callback(
                "SASL2 Success",
                MatchXPath(SASL2Success.tag_name()),
                self._handle_success,
                instream=True,
            )
        )
        self.xmpp.register_handler(
            Callback(
                "SASL2 Failure",
                MatchXPath(SASL2Failure.tag_name()),
                self._handle_failure,
                instream=True,
            )
        )
        self.xmpp.register_handler(
            Callback(
                "SASL2 Continue",
                MatchXPath(SASL2Continue.tag_name()),
                self._handle_continue,
                instream=True,
            )
        )

        self.xmpp.register_feature(
            "sasl2",
            self._handle_sasl2_feature,
            restart=True,
            order=self.config.get("order", 90),
        )

    # -----------------------------------------------------------------------
    # Extension-point registration
    # -----------------------------------------------------------------------

    def set_mechanism_override(self, fn: Callable):
        """
        Register a callback that may override mechanism selection.

        fn(server_features: SASL2Feature) → (mech_name, initial_response) | None
        """
        self._mechanism_override = fn

    def add_authenticate_hook(self, fn: Callable):
        """fn(auth_stanza: SASL2Authenticate, server_features: SASL2Feature)"""
        self._authenticate_hooks.append(fn)

    # -----------------------------------------------------------------------
    # Stream feature handler
    # -----------------------------------------------------------------------

    def _handle_sasl2_feature(self, features):
        log.error("_handle_sasl2_feature", features)
        if "sasl2" in self.xmpp.features:
            return False

        feature = features["sasl2"]
        server_mechs = feature["mechanisms"]
        log.debug("SASL2: server mechanisms: %s", server_mechs)

        # Let an inline-feature plugin (e.g. FAST) take over mechanism selection.
        if self._mechanism_override:
            override = self._mechanism_override(feature)
            if override is not None:
                mech_name, initial_response = override
                self._send_authenticate(mech_name, initial_response, feature)
                return True

        # Default: use slixmpp's existing SASL machinery.
        return self._authenticate_standard(server_mechs, feature)

    def _authenticate_standard(self, server_mechs, feature) -> bool:

        mech_plugin = self.xmpp.plugin.get("feature_mechanisms")
        if mech_plugin is None:
            log.error(
                "SASL2: feature_mechanisms plugin not loaded "
                "(needed for credential callbacks)"
            )
            return False

        try:
            mech = sasl.choose(
                server_mechs,
                mech_plugin.sasl_callback,
                mech_plugin.security_callback,
                limit=mech_plugin.use_mechs,
                min_mech=mech_plugin.min_mech,
            )
        except sasl.SASLNoAppropriateMechanism:
            log.error("SASL2: no appropriate mechanism in %s", server_mechs)
            self.xmpp.event("failed_all_auth")
            return False

        try:
            initial_data = mech.process()
        except sasl.SASLCancelled:
            log.error("SASL2: mechanism %s cancelled", mech.name)
            return False

        self._pending_mech = mech
        self._send_authenticate(mech.name, initial_data or b"", feature)
        return True

    def _send_authenticate(self, mech_name: str, initial_response: bytes, feature):
        auth = SASL2Authenticate(self.xmpp)
        auth["mechanism"] = mech_name
        auth["initial_response"] = initial_response

        for hook in self._authenticate_hooks:
            hook(auth, feature)

        log.debug("SASL2: sending authenticate mech=%s", mech_name)
        auth.send()

    # -----------------------------------------------------------------------
    # Server response handlers
    # -----------------------------------------------------------------------

    def _handle_success(self, stanza):
        log.debug("SASL2: success")

        self.xmpp.authenticated = True
        self.xmpp.features.add("sasl2")
        self.xmpp.event("sasl2_success", stanza)
        self.xmpp.init_parser()
        self.xmpp.send_raw(self.xmpp.stream_header)

    def _handle_failure(self, stanza):
        condition = stanza.get_condition()
        log.warning("SASL2: failure: %s %s", condition, stanza.get_text())

        self.xmpp.event("sasl2_failed", stanza)
        self.xmpp.disconnect()

    def _handle_continue(self, stanza):
        """Handle a SASL2 <continue> for multi-step mechanisms (e.g. SCRAM)."""
        mech = self._pending_mech
        if mech is None:
            log.error("SASL2: received <continue> but no pending mechanism")
            return

        try:
            response_data = mech.process(stanza.get_additional_data())
        except sasl.SASLMutualAuthFailed:
            log.error("SASL2: mutual auth failed")
            self.xmpp.disconnect()
            return
        except sasl.SASLFailed as exc:
            log.error("SASL2: mechanism failed: %s", exc)
            self.xmpp.disconnect()
            return

        resp = SASL2Response(self.xmpp)
        resp["additional_data"] = response_data or b""
        resp.send()


# Register in slixmpp's global plugin registry when this module is imported,
# so that plugins which list 'xep_0388' as a dependency can resolve it.
register_plugin(xep_0388)
