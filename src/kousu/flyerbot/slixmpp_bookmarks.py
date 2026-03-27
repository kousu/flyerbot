"""
Improved slixmpp bookmarks module.

https://xmpp.org/extensions/xep-0402.html
"""

import asyncio
import logging
from random import randint
from itertools import count
from typing import List

from slixmpp import JID, Message
from slixmpp.types import PresenceArgs
import slixmpp.plugins.xep_0402
from slixmpp.plugins.xep_0060.stanza.pubsub import Item, Items
from slixmpp.plugins.xep_0402.stanza import Conference as Bookmark
from slixmpp.exceptions import IqError
from slixmpp.plugins.base import register_plugin

from . import util

log = logging.getLogger(__name__)

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
        "jid": None, # if None, defaults to the JID; but if not, will
        "autojoin": True,  # if true, our presence in rooms is synced with the autojoin flags in our bookmarks (most modern XMPP clients do this so that all clients see the same world)
        # For these parameters, see https://slixmpp.readthedocs.io/en/latest/api/plugins/xep_0045.html#slixmpp.plugins.xep_0045.XEP_0045.join_muc_wait
        "maxchars": None,  # If autojoin, max number of characters to return from history, if autojoining.
        "maxstanzas": None, # If autojoin, max number of stanzas to return from history.
        "seconds": None,  # If autojoin, fetch history until that many seconds in the past.
        "since": None,  # If autojoin, fetch history since that timestamp.
        "timeout": 300, # If autojoin, timeout after which a TimeoutError is raised. None means no timeout.
    }

    bookmarks: dict[JID, Bookmark]

    def plugin_init(self):
        if self.jid is None:
            self.jid = self.xmpp.boundjid.bare

        if self.jid != self.xmpp.boundjid.bare and self.xmpp.is_component:
            log.warn(f"Tracking bookmarks of other JIDs probably only works if we are a component and they are our authorized children. Make sure that {self.jid} is on the same domain as {self.xmpp.boundjid}.")

        super().plugin_init()

        self.xmpp.add_event_handler("session_start", self._on_start)
        self.xmpp.add_event_handler("groupchat_presence", self._on_groupchat_presence)

        self.bookmarks = {}
        # map_node_event adds "bookmark" + "_" + {"publish","retract"} events to the pubsub plugin
        self.xmpp["xep_0060"].map_node_event(
            slixmpp.plugins.xep_0402.stanza.NS, "bookmark"
        )
        self.xmpp.add_event_handler("bookmark_publish", self._on_bookmarks_changed)
        self.xmpp.add_event_handler("bookmark_retract", self._on_bookmarks_retracted)

    def session_bind(self, jid):
        # I don't understand this incantation; copied from slixmpp/plugins/xep_0292/vcard4.py
        # it also doesn't seem to .. exist ?
        self.xmpp["xep_0163"].register_pep(
            "bookmark", slixmpp.plugins.xep_0402.stanza.Conference
        )

    def plugin_end(self):
        # I don't understand this incantation; copied from slixmpp/plugins/xep_0292/vcard4.py
        self.xmpp["xep_0030"].del_feature(feature=slixmpp.plugins.xep_0402.stanza.NS)
        self.xmpp["xep_0163"].remove_interest(slixmpp.plugins.xep_0402.stanza.NS)

        for plugin in self._children.values():
            plugin.plugin_end()

    async def _on_start(self, event):
        await self._sync_bookmarks()

    async def _on_groupchat_presence(self, presence):
        """
        """

        if 110 not in presence["muc"]["status_codes"]:
            # 110 = "Self Presence", and it specifically means _for this device_.
            # This filters out presences for other users and for other devices on our account.
            return

        if 201 in presence["muc"]["status_codes"]:
            # Configure rooms we create so they are usable. code 201 = "Created".
            #
            # https://xmpp.org/extensions/xep-0045.html#createroom-general:
            # > The initial presence stanza received by the owner from the room
            # > MUST include extended presence information indicating the user's
            # > status as an owner and acknowledging that the room has been
            # > created (via status code > 201) and is awaiting configuration.

            # i.e. to approve and unlock the new room the bare minimum is just
            # to re-send the default config back.
            muc_jid = presence["from"].bare
            form = await self.xmpp["xep_0045"].get_room_config(muc_jid)
            # TODO:
            # - set persistent?
            # - set members only?
            # - what does Cheogram do on bookmarks associated with destroyed rooms?
            await self.xmpp["xep_0045"].set_room_config(muc_jid, config=form, ifrom=presence['to'])

        BANNED = 301
        KICKED = 307
        LOST_MEMBERSHIP = 321
        MEMBERS_ONLY = 322
        if presence["type"]=="unavailable":
            if ({BANNED, KICKED, LOST_MEMBERSHIP, MEMBERS_ONLY} & presence["muc"]["status_codes"]):
                # we got kicked out.
                if self.config["autojoin"]:
                    # sync bookmark with this information so we don't uselessly rejoin
                    current = self.get(presence["from"].bare)
                    self.add(presence["from"].bare, current["nick"], current["name"], autojoin=False, password=current["password"])
            else:
                # schedule a rejoin
                t = asyncio.create_task(self._sync_muc(presence["from"].bare))
                self.add_event_handler("session_end", lambda _: t.cancel())

    async def _sync_muc(self, muc_jid: JID | str, user_jid: JID | None = None):
        """
            Helper: join/leave a single MUC according to the bookmarks state:
            - if config["autojoin"] and bookmarks["muc_jid"]["autojoin"],
            muc_jid is entered;
            - if config["autojoin"] and bookmarks[muc_jid]["autojoin"].
            *blocks until it succeeds*, so run this in a background Task.
        """
        # TODO: put a per-muc lock around this?
        if not self.config["autojoin"]:
            return

        for attempt in count():
            # loop until we're connected or our bookmark no longer instructs us to be
            # get_joined_rooms() forgets to check for multi_from:
            user_jid = user_jid if self.xmpp["xep_0045"].multi_from else None
            rooms = self.xmpp["xep_0045"].get_joined_rooms(user_jid)
            bookmark = self.bookmarks.get(muc_jid)
            if bookmark is None or not bookmark["autojoin"]:
                # leave
                if muc_jid in rooms:
                    nick = JID(
                        self.xmpp["xep_0045"].get_our_jid_in_room(muc_jid)
                    ).resource
                    log.info("Leaving %s as %s", muc_jid, nick)
                    self.xmpp["xep_0045"].leave_muc(muc_jid, nick)
            else:
                # join
                try:
                    if muc_jid not in rooms:
                        log.info("Joining %s as %s", muc_jid, nick)
                        await self.xmpp["xep_0045"].join_muc_wait(
                            muc_jid,
                            bookmark["nick"] or self.xmpp.boundjid.user,
                            password=bookmark["password"] or None,
                            maxchars=self.config["maxchars"],
                            maxstanzas=self.config["maxstanzas"],
                            seconds=self.config["seconds"],
                            since=self.config["since"],
                            timeout=self.config["timeout"],
                        )
                except TimeoutError:
                    log.warn("Timed out joining %s on attempt %d; sleeping before retrying..", muc_jid, attempt+1)
                    # exponential backoff with some jitter to avoid thundering-herds
                    await asyncio.sleep(min(10 * (1+0.5)**attempt, 600) + randint(20,60))
                    continue
                break


    # I wish
    async def _upsert_bookmarks(self, items: Items | list[Item]):
        for item in items:
            if "conference" not in item:
                raise TypeError(f"<item> {item} should have contained a <conference>")

        async with asyncio.TaskGroup() as tg:
            for item in items:
                room = JID(item["id"])
                self.bookmarks[room] = item["conference"]
                tg.create_task(self._sync_muc(room))

    async def _delete_bookmarks(self, items: Items | list[Item]):
        for item in items:
            room = JID(item["id"])
            if room not in self.bookmarks:
                raise ValueError(f"{room} was not in our bookmarks cache.")

        async with asyncio.TaskGroup() as tg:
            for item in items:
                room = JID(item["id"])
                del self.bookmarks[room]
                tg.create_task(self._sync_muc(room))

    async def _on_bookmarks_changed(self, msg: Message):
        # triggered on both creations/additions
        self.xmpp.event("bookmarks_changed", msg)
        await self._upsert_bookmarks(msg["pubsub_event"]["items"])

    async def _on_bookmarks_retracted(self, msg: Message):
        # triggered on deletions
        self.xmpp.event("bookmarks_removed", msg)
        await self._delete_bookmarks(msg["pubsub_event"]["items"])

    async def _sync_bookmarks(self):
        """
        Query the server for the current list of bookmarks and save them to the local cache.
        """
        try:
            result = await self.xmpp["xep_0060"].get_items(
                self.jid, slixmpp.plugins.xep_0402.stanza.NS
            )

            await self._upsert_bookmarks(result["pubsub"]["items"])

            # compute the delta; more complicated than just leaving all rooms but it minimizes network traffic
            deletions = set(self.bookmarks) - set(JID(item["id"]) for item in result["pubsub"]["items"])
        except IqError as exc:
            if exc.condition == "item-not-found":
                # there are no bookmarks anymore
                # desync everything
                deletions = set(self.bookmarks)
            else:
                log.error("Unable to retrieve PEP-native bookmarks: %s", exc)
                raise

        if deletions:
            # python set() -> <Items> XML tag
            _deletions = Items()
            for room in deletions:
                item = Item()
                item["id"] = room
                item["conference"] = self.bookmarks[room] # this isn't actually necessary, but here for completeness
                _deletions.append(item)
            deletions = _deletions; del _deletions

            await self._delete_bookmarks(deletions)


    async def add(self, muc_jid, nick=None, name=None, autojoin=True, password=None):
        """
        Add/edit a MUC bookmark.

        muc_jid - the groupchat to join e.g. room@conference.jabber.org
        name - the local name you have for this room e.g. "Jabber Heads"
        nick - the nickname you will have in this room (when messaging in a MUC you are room@conference.jabber.org/nick)
        password - if the room is password-protected, this is the password

        The server should reflect this back to us once it accepts it, and when
        that happens, if config["autojoin"] is on, this will cause a join.
        """
        muc_jid = JID(muc_jid)

        item = slixmpp.plugins.xep_0060.stanza.Item()
        item["id"] = muc_jid
        item["conference"]["nick"] = nick or self.xmpp.boundjid.user
        item["conference"]["name"] = name or muc_jid.user
        item["conference"]["autojoin"] = autojoin
        if password:
            item["conference"]["password"] = password

        log.info("Adding bookmark %s: %s", muc_jid, item)
        await self.xmpp["xep_0060"].publish(
            self.jid,
            slixmpp.plugins.xep_0402.stanza.NS,
            id=muc_jid,
            payload=item["conference"].xml,
        )

    async def get(self, muc_jid) -> Bookmark:
        return self.bookmarks[muc_jid]

    async def remove(self, muc_jid):
        """
        Remove a bookmark.

        The server should reflect this back to us once it accepts it, and when
        that happens, if config["autojoin"] is on, this will cause a leave.
        """
        muc_jid = str(JID(muc_jid))  # normalize case how XMPP wants

        log.info("Retracting bookmark %s", muc_jid)
        await self.xmpp["xep_0060"].retract(
            self.jid,
            slixmpp.plugins.xep_0402.stanza.NS,
            id=muc_jid,
            notify=True,
        )

    def register(self, jid):
        """
            Track bookmarks of a specific user.
            This is usually only allowed if we are a component; then we can impersonate any JID under our domain.
            If used on a jid that's not part of our component no error will be raised
            but nothing will happen.
        """
        if self.jid != self.xmpp.boundjid.bare:
            raise ValueError("This is already a child plugin. It cannot track children.")
        if not self.xmpp["xep_0045"].multi_from:
            log.warn(f"Registering XEP-0402 to track bookmarks of {jid} will probably messily join all  because XEP-0045 multi_from is disabled.")
        assert str(jid) == str(JID(jid).bare), "You must pass a bare JID"
        # the way this works is
        if jid not in self._children:
            cls = type(self)
            self._children[jid] = cls(self.xmpp, {**self.config, jid: jid})
            self._children[jid].plugin_init()



# NB: this *prevents* slixmpp.plugins.xep_0402 from registering itself
register_plugin(XEP_0402)
