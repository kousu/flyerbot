import os
import re
import io
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import traceback
import logging

import xdg.BaseDirectory
import aiohttp
import asyncio
import slixmpp
import slixmpp_omemo
from typing import Tuple
from omemo.session_manager import NoEligibleDevices

import kousu.flyerocr
import kousu.flyerocr.util

from . import slixmpp_fast
from . import slixmpp_omemo  # imported to trigger the register_plugin()
from . import slixmpp_bookmarks
from . import util

log = logging.getLogger(__name__)

# TODO:
# - [ ] accept jingle file transfers too? for people who don't have http_upload?
# - [ ] accept base64-embedded OOB images too?
# - [ ] add a 'check your Claude balance' command (not supported directly by the Claude API but there is a billing history API so you can guess)

class FlyerBot(slixmpp.ClientXMPP):
    """
    XMPP bot that receives flyer images, OCRs them, and returns .ics files.

    Supports:
    - XEP-0363 (HTTP File Upload) for uploading .ics files
    - XEP-0066 (Out-of-Band Data) for sharing image and .ics URLs
    - XEP-0384 (OMEMO) for encrypted messaging (always enabled)
    - XEP-0484 (FAST) for SASL FAST authentication
    """

    def __init__(self, jid, password=None):
        super().__init__(jid, password)

        # TODO: sanitize jid to be filename safe

        state = xdg.BaseDirectory.save_data_path("flyerbot", jid)

        # these two semaphores nest:
        # the first is the outer one which provides backpressure to callers
        # the second is the one that guards workers
        self._queue = asyncio.BoundedSemaphore(10)
        self._workers = asyncio.BoundedSemaphore(2)
        # (gosh I was going to try to do this with queues and worker tasks and futures
        # but sempahores are ...a lot simpler actually)

        # Enable relevant XMPP features
        #

        # OOB - Out-of-Band Data
        self.register_plugin("xep_0066")

        # Replies
        self.register_plugin("xep_0461")
        self.register_plugin("xep_0359")
        self.register_plugin("xep_0428")

        # HTTP File Upload
        self.register_plugin("xep_0363")

        # Bookmarks (i.e. groupchat roster)
        self.register_plugin(
            "xep_0402"
        )  # , add {'autojoin': False, 'maxhistory': 20} as a second argument to not sync bookmarks module=slixmpp_bookmarks)

        # OMEMO (encrypted messaging)
        self.register_plugin("xep_0384", {"storage": os.path.join(state, "omemo")})
        self.register_plugin("xep_0454")

        # FAST (SASL FAST authentication)
        # self.register_plugin(
        # "xep_0484", {"storage": os.path.join(state, "fast-token.json")}
        # )

        # Event handlers
        #
        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.message)

    async def start(self, event):
        """Initialize session - send presence, enable roster."""
        self.send_presence()
        await self.get_roster()

    async def message(self, msg):
        """
        Handle incoming messages.
        - Collect all image URLs from OOB data and message body
        - De-duplicate using dict (preserves insertion order in Python 3.7+)
        - For each image: download, OCR, reply with .ics
        """

        if msg["type"] not in ("chat", "normal", "groupchat"):
            return

        if msg["delay"]["stamp"]:
            # scrollback; not a live message; ignore
            return

        if (
            msg["type"] == "groupchat"
            and self["xep_0045"].get_our_jid_in_room(msg["from"].bare) == msg["from"]
        ):
            # don't talk to ourself (in MUCs)
            return

        if msg["from"].bare == self.boundjid.bare:
            # don't talk to ourself
            return

        log.info("Message from %s", msg["from"])

        # decrypt OMEMO
        omemo_peer = None
        if self["xep_0384"].is_encrypted(msg):
            msg, omemo_peer = await self["xep_0384"].decrypt_message(msg)
            # sender_jid = omemo_peer.bare_jid
            # TODO: should we use omemo_peer.bare_jid instead of msg['from'].bare?
            #  presumably that's unforgeable.
            # # there's also omemo_peer.device_id ?

        reply_id = util.reply_id(msg)

        # decide if this person is authorized to use us:
        # - they're in a group with us
        #   - this might be overkill, since if they're not in a group with us we can't hear their messages
        #     but maybe there's some crazy forgery attack against XMPP that lets people message rooms they're not in.
        # - they're on our buddy list (with subscription 'from', meaning _we send our presence with them_)
        friends = (msg["type"] in ["chat", "normal"]) and (
            self.roster[self.boundjid.bare][msg["from"]]["from"]
        )
        groupchat = msg["type"] == "groupchat" and (
            msg["from"].bare in util.xep_0045_rooms(self, msg["to"])
        )
        allowed = bool(friends or groupchat)

        nickname = self.boundjid.user
        if msg["type"] == "groupchat":
            nickname = slixmpp.JID(
                self["xep_0045"].get_our_jid_in_room(msg["from"].bare)
            ).resource  # <-- this seems like something the xep_0045 plugin could do..

        # menu
        # - 'help' - show the instructions
        cmd = None
        cmd = msg["body"].lower().strip()
        if msg["type"] == "groupchat":
            # in a groupchat, only respond if mentioned
            # this is super verbose but i don't want to use a regex because nickname is potentially attacker-controlled
            if cmd.startswith(f"@{nickname.lower()}: "):
                cmd = cmd[1+len(nickname)+2:]
            elif cmd.startswith(f"@{nickname.lower()}, "):
                cmd = cmd[1+len(nickname)+2:]
            elif cmd.startswith(f"@{nickname.lower()} "):
                cmd = cmd[1+len(nickname)+1:]
            elif cmd.startswith(f"{nickname.lower()}: "):
                cmd = cmd[len(nickname)+2:]
            elif cmd.startswith(f"{nickname.lower()}, "):
                cmd = cmd[len(nickname)+2:]
            elif cmd.startswith(f"{nickname.lower()} "):
                cmd = cmd[len(nickname)+1:]
            else:
                cmd = ""
        cmd = cmd.strip()

        if cmd == "help":
            _msg = f"I am {nickname.title()} and I can turn photographs of event flyers into importable iCal files. "

            if allowed:
                _msg += "Just send me a snapshot or a link to your poster or pancarte!"
            else:
                _msg += "If you would like me to scan flyers for you, ask my owner to introduce us."
            reply = msg.reply(_msg)
            reply["reply"]["to"] = msg["from"]
            reply["reply"]["id"] = reply_id
            await self._send(reply, encrypt=omemo_peer is not None)
            return

        ## Gather the input
        # Collect (unique) image URLs, while preserving orderee
        image_urls = {}

        # Read OOB data (XEP-0066)
        if "oob" in msg and "url" in msg["oob"]:
            image_urls[msg["oob"]["url"]] = True

        # Read image URLs in message body
        # (usually these repeat the oob url)
        if msg.get("body"):
            for url_match in re.finditer(r"(https?|aesgcm)://[^\s]+", msg["body"]):
                image_urls[url_match.group(0)] = True

        ## Reject people
        # (but only if they tried to use us; empty messages are just ignored)

        if image_urls and not allowed:
            await self._send(
                msg.reply(
                    "We are not friends. If you would like me to scan flyers for you, ask my owner to introduce us. 🐗."
                ),
                encrypt=omemo_peer is not None,
            )
            return

        # Process each image in order encountered, send separate .ics for each
        ics_title = ics_url = None
        for url in image_urls.keys():
            reply = msg.reply()
            ## Add xep-0461 Reply
            reply["reply"]["to"] = msg["from"]
            reply["reply"]["id"] = reply_id

            try:
                ics_title, ics_url = await self._process_image_url(
                    url, encrypt=omemo_peer is not None
                )
                reply["body"] = ics_url
                reply["subject"] = ics_title
                # oob is ignored by most clients when OMEMO is used
                # but it's necessary in the unencrypted case to render as downloadable files
                # except that some clients detect URL-only messages and render them as if they were attachments 😵‍💫
                reply["oob"]["url"] = ics_url
            except ResourceWarning as exc:
                reply["body"] = f"{exc}"
                await self._send(reply, encrypt=omemo_peer is not None)
                # END here to cut short hammering on an overworked box
                # (and avoid repeating the error to the user)
                break
            except kousu.flyerocr.NotImageError:
                if msg["type"] != "groupchat":
                    # don't be noisy in groups about irrelevant details
                    reply["body"] = "Not an image."
            except kousu.flyerocr.NotEventError:
                if msg["type"] != "groupchat":
                    # don't be noisy in groups about irrelevant details
                    reply["body"] = "Not an event."
            except Exception as exc:
                reply["body"] = f"{exc}"

                _cause = exc.__cause__
                while _cause:
                    reply["body"] += f": {type(_cause).__name__}" + (
                        f": {_cause}" if str(_cause) else ""
                    )
                    _cause = _cause.__cause__

                log.error("Image %s caused:\n%s", url, traceback.format_exc())

            if reply["body"]:
                await self._send(reply, encrypt=omemo_peer is not None)

    async def _process_image_url(self, url: str, encrypt: bool = True):

        if self._queue.locked():
            raise ResourceWarning(
                f"All of my {self._queue._bound_value:d} slots are full and waiting their turn at the moment. Try again in a bit."
            )

        # if more than 10 requests, they should be rejected by the _queue.locked() check
        # if somehow one slips by due to race conditions, they will block here and wait their turn
        async with self._queue:
            # Up to 10 requests could end up here concurrently
            async with self._workers:
                # but only 2 can be here concurrently; the other 8 will be blocked
                return await asyncio.wait_for(
                    self.__process_image_url(url, encrypt=encrypt), timeout=47
                )

    async def __process_image_url(
        self, url: str, encrypt: bool = True
    ) -> Tuple[str, str]:
        """
        Download image from URL, run OCR, return event title and uploaded file (which might be uploaded as an encrypted aesgcm:// file)
        Called once per image URL (deduplication handled in message()).
        """

        ## Input
        image_data = await self._fetch(url)

        ## Processing
        #
        log.info(f"Processing image: {url}")
        try:
            event_dict = kousu.flyerocr.ocr.ocr_flyer(image_data)
        except kousu.flyerocr.NotEventError:
            raise
        except Exception as exc:
            raise Exception("OCR Error") from exc

        try:
            ics_content = kousu.flyerocr.ics.make(event_dict)
            ics_content = ics_content.encode("utf-8")
        except Exception as exc:
            raise Exception("Processing failed.", event_dict) from exc

        ## Output
        #
        try:
            ics_url = await self["xep_0454" if encrypt else "xep_0363"].upload_file(
                Path(kousu.flyerocr.util.filename(event_dict)),
                len(ics_content),
                "application/octet-stream" if encrypt else "text/calendar",
                input_file=io.BytesIO(ics_content),
            )
        except Exception as exc:
            raise Exception("Error uploading event") from exc

        return event_dict.get("title"), ics_url

    async def _fetch(self, url):
        encryption_key = None
        if url.startswith("aesgcm://"):
            url = urlsplit(url)
            encryption_key = url.fragment
            url = urlunsplit(("https", url.netloc, url.path, None, None))
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                image_data = await resp.read()
        if encryption_key:
            image_data = self["xep_0454"].decrypt(
                io.BytesIO(image_data), encryption_key
            )

        return image_data

    async def _send(self, msg, encrypt=True):
        """
        Send a message possibly (hopefully) with OMEMO.
        """

        if not encrypt:
            msg.send()
            return
        else:
            try:
                msgs, _ = await self["xep_0384"].encrypt_message(msg, msg["to"])

                for omemo_version, _msg in msgs.items():
                    if omemo_version == "eu.siacs.conversations.axolotl":
                        # "oldmemo" doesn't encrypt surrounding tags, so py-omemo
                        # strips them for safety and we have to readd them
                        _msg["oob"]["url"] = msg["oob"]["url"]

                        _msg["reply"]["id"] = msg["reply"]["id"]
                        _msg["reply"]["to"] = msg["reply"]["to"]
                    _msg.send()
            except NoEligibleDevices:
                log.warn("Encryption requested to %s but keys unavailable.", msg["to"])
                # Fall back to unencrypted
                reply = self["xep_0461"].make_reply(
                    msg["reply"]["to"].bare,
                    msg["reply"]["id"],
                    mto=msg["to"],
                    mtype="chat",
                    mbody="I don't know your OMEMO encryption keys.",
                )
                reply.send()
