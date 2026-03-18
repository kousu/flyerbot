"""
Improved slixmpp bookmarks module.

https://xmpp.org/extensions/xep-0402.html
"""

import asyncio
import logging
from typing import List

from slixmpp import JID
import slixmpp.plugins.xep_0402
from slixmpp.exceptions import IqError
from slixmpp.plugins.base import register_plugin

from . import util

log = logging.getLogger(__name__)

# TODO:
# - [ ] track a (read only) .bookmarks property, similar to xmpp["xep_0045"].rooms ?
# - [ ] put things on Tasks
# - [ ] support the other parameters to https://slixmpp.readthedocs.io/en/latest/api/plugins/xep_0045.html#slixmpp.plugins.xep_0045.XEP_0045.join_muc_wait as config options
# - [ ] puppeting madness


class XEP_0402(slixmpp.plugins.xep_0402.XEP_0402):
    """
        The XEP_0402 plugin expanded to actually have an API to
        and to respect autojoin (like XEP_0048 does).
        Also, listens for autojoin changes and syncs its state,
        if allowed by the user.
    """

    name = "xep_0402"
    description = "XEP-0402: PEP native Bookmarks"
    dependencies = slixmpp.plugins.xep_0402.XEP_0402.dependencies | {
        "xep_0045",
        "xep_0060",
    }

    default_config = {
        "autojoin": True,  # if true, our presence in rooms is synced with the autojoin flags in our bookmarks (most modern XMPP clients do this so that all clients see the same world)
        "maxstanzas": 0,  # how many messages of scrollback history to request when autojoining rooms
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def plugin_init(self):
        super().plugin_init()
        self.xmpp.add_event_handler("session_start", self._on_start)
        self.xmpp.add_event_handler("groupchat_presence", self._on_groupchat_presence)

        # map_node_event adds "bookmark" + "_" + {"publish","retract"} events to the pubsub plugin
        self.xmpp["xep_0060"].map_node_event(
            slixmpp.plugins.xep_0402.stanza.NS, "bookmark"
        )
        self.xmpp.add_event_handler("bookmark_publish", self._on_bookmark_changed)
        self.xmpp.add_event_handler("bookmark_retract", self._on_bookmark_retracted)

    def session_bind(self, jid):
        # I don't understand this incantation; copied from slixmpp/plugins/xep_0292/vcard4.py
        self.xmpp["xep_0163"].register_pep(
            "bookmark", slixmpp.plugins.xep_0402.stanza.Conference
        )

    def plugin_end(self):
        # I don't understand this incantation; copied from slixmpp/plugins/xep_0292/vcard4.py
        self.xmpp["xep_0030"].del_feature(feature=slixmpp.plugins.xep_0402.stanza.NS)
        self.xmpp["xep_0163"].remove_interest(slixmpp.plugins.xep_0402.stanza.NS)

    async def _on_start(self, event):
        await self._autojoin(self.xmpp.boundjid, await self.bookmarks)

    async def _on_groupchat_presence(self, presence):
        """
        Configure rooms we create so they are usable.
        https://xmpp.org/extensions/xep-0045.html#createroom-general
        """

        if 201 in presence["muc"]["status_codes"]:
            # code 201 = "Created" i.e. by us and we should also be the Owner
            # https://xmpp.org/extensions/xep-0045.html#createroom-general
            # >  If this user is allowed to create a room and the room does not yet exist,
            # > the service MUST create the room according to some default configuration,
            # > assign the requesting user as the initial room owner, and add the owner to
            # > the room but not allow anyone else to enter the room (effectively "locking"
            # > the room). The initial presence stanza received by the owner from the room
            # > MUST include extended presence information indicating the user's status as
            # > an owner and acknowledging that the room has been created (via status code
            # > 201) and is awaiting configuration.

            # i.e. to approve and unlock the new room the bare minimum is just
            # to re-send the default config back.
            muc_jid = presence["from"].bare
            form = await self.xmpp["xep_0045"].get_room_config(muc_jid)
            # TODO:
            # - set persistent?
            # - set members only?
            # - what does Cheogram do on bookmarks associated with destroyed rooms?
            await self.xmpp["xep_0045"].set_room_config(muc_jid, config=form)

    async def _on_bookmark_changed(self, msg):
        self.xmpp.event("bookmark_changed", msg)

        assert msg["to"] == self.xmpp.boundjid
        await self._autojoin(msg["to"], msg["pubsub_event"]["items"])

    async def _on_bookmark_retracted(self, msg):
        self.xmpp.event("bookmark_removed", msg)

        assert msg["to"] == self.xmpp.boundjid
        await self._autojoin(msg["to"], msg["pubsub_event"]["items"])

    async def _autojoin(
        self, jid, conferences: List[slixmpp.plugins.xep_0060.stanza.Item]
    ):
        """
        If autojoin is enabled in self.config, synchronize
        the live states with the bookmarks.

        i.e. autojoin on a bookmark means you're in the room at "all" times,
             and not autojoin means you're never in it.
        """
        if not self.config["autojoin"]:
            return

        rooms = util.xep_0045_rooms(self.xmpp, jid)

        async def _t(item):
            muc_jid = item["id"]
            if item.name == "item":
                nick = item["conference"]["nick"] or self.xmpp.boundjid.user
                autojoin = item["conference"]["autojoin"]
                password = item["conference"]["password"] or None
            elif item.name == "retract":
                # a bit of a hack: treat <retract>s as if they are bookmarks with autojoin="False"
                nick = JID(
                    self.xmpp["xep_0045"].get_our_jid_in_room(JID(item["id"]))
                ).resource  # <-- this seems like something the xep_0045 plugin could do..
                autojoin = False
                password = None

            if autojoin and muc_jid not in rooms:
                log.info("Joining %s as %s", muc_jid, nick)
                await self.xmpp["xep_0045"].join_muc_wait(
                    muc_jid,
                    nick,
                    password=password,
                    maxstanzas=self.config["maxstanzas"],
                )
            elif not autojoin and muc_jid in rooms:
                log.info("Leaving %s as %s", muc_jid, nick)
                self.xmpp["xep_0045"].leave_muc(muc_jid, nick)

        for item in conferences:
            # join/leave everything in parallel
            asyncio.create_task(_t(item))
            # await _t(item) # <-- serial version, for debugging

    @property
    async def bookmarks(self) -> List[slixmpp.plugins.xep_0060.stanza.Item]:
        """
        The list of current bookmarks.
        """
        # TODO: cache this; use _on_bookmark_{changed,retracted} to update the cache.
        try:
            result = await self.xmpp["xep_0060"].get_items(
                self.xmpp.boundjid.bare, slixmpp.plugins.xep_0402.stanza.NS
            )
            return result["pubsub"]["items"]
        except IqError as exc:
            if exc.condition == "item-not-found":
                return []
            else:
                log.error("Unable to retrieve PEP-native bookmarks: %s", exc)
                raise

    async def add(self, muc_jid, nick=None, name=None, autojoin=True, password=None):
        """
        Add/edit a MUC bookmark.

        muc_jid - the groupchat to join e.g. room@conference.jabber.org
        name - the local name you have for this room e.g. "Jabber Heads"
        nick - the nickname you will have in this room (when messaging in a MUC you are room@conference.jabber.org/nick)
        password - if the room is password-protected, this is the password
        """
        muc_jid = JID(muc_jid)

        item = slixmpp.plugins.xep_0060.stanza.Item()
        item["id"] = muc_jid
        item["conference"]["name"] = name or muc_jid.user
        item["conference"]["autojoin"] = autojoin
        item["conference"]["nick"] = nick or self.xmpp.boundjid.user
        if password:
            item["conference"]["password"] = password

        log.info("Adding bookmark %s: %s", muc_jid, item)
        await self.xmpp["xep_0060"].publish(
            self.xmpp.boundjid.bare,
            slixmpp.plugins.xep_0402.stanza.NS,
            id=muc_jid,
            payload=item["conference"].xml,
        )

    async def remove(self, muc_jid):
        """
        Remove a bookmark.

        If autojoin is set this will also leave the room.
        """
        muc_jid = str(JID(muc_jid))  # normalize case how XMPP wants

        log.info("Retracting bookmark %s", muc_jid)
        await self.xmpp["xep_0060"].retract(
            self.xmpp.boundjid.bare,
            slixmpp.plugins.xep_0402.stanza.NS,
            id=muc_jid,
            notify=True,
        )


# NB: this *prevents* slixmpp.plugins.xep_0402 from registering itself
register_plugin(XEP_0402)
