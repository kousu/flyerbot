import os
import io
import json
import logging
import secrets
from typing import Optional, FrozenSet

import aiohttp
import omemo.storage
import slixmpp.plugins
import slixmpp_omemo
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)


class XEP_0384(slixmpp_omemo.XEP_0384):
    # due to storage/blind-trust-before-verification having no default values
    # this plugin does *not* auto-register. It's an abstract base class and you
    # have to implement the missing pieces then register it.
    default_config = {
        "fallback_message": "This message is OMEMO encrypted.",
        "storage": None,
    }

    @property
    def storage(self):
        return self._storage

    def plugin_init(self) -> None:
        if not self.config.get("storage"):
            raise Exception("xep_0384: storage must be specified at register_plugin")

        self._storage = type(self).Storage(self.config["storage"])

        super().plugin_init()

    @property
    def _btbv_enabled(self) -> bool:
        # blind-trust-before-verification
        return True

    async def _devices_blindly_trusted(
        self,
        blindly_trusted: FrozenSet[omemo.DeviceInformation],
        identifier: Optional[str],
    ) -> None:
        log.info(f"[{identifier}] Devices trusted blindly: {blindly_trusted}")

    async def _prompt_manual_trust(
        self,
        manually_trusted: FrozenSet[omemo.DeviceInformation],
        identifier: Optional[str],
    ) -> None:
        # All devices should be automatically trusted blindly by BTBV
        # so this should never be called.
        raise NotImplementedError()

        # To show how a full implementation could look like, the following code will prompt for a trust
        # decision using `input`:
        # session_mananger = await self.get_session_manager()

        # for device in manually_trusted:
        #     while True:
        #         answer = input(f"[{identifier}] Trust the following device? (yes/no) {device}")
        #         if answer in { "yes", "no" }:
        #             await session_mananger.set_trust(
        #                 device.bare_jid,
        #                 device.identity_key,
        #                 TrustLevel.TRUSTED.value if answer == "yes" else TrustLevel.DISTRUSTED.value
        #             )
        #             break
        #         print("Please answer yes or no.")

    class Storage(omemo.storage.Storage):
        """
        OMEMO key storage using JSON files in XDG_STATE_HOME.
        """

        def __init__(self, storage_path, disable_cache=False):
            super().__init__(disable_cache=disable_cache)
            self._path = storage_path
            self._db = None

        async def load_optional(self, key, _type):
            v = await self._load(key)
            if isinstance(v, omemo.storage.Nothing):
                return omemo.storage.Just(None)
            return v

        async def _load(self, key):
            key = os.path.relpath(os.path.normpath(os.path.join("/", key)), "/")
            path = os.path.join(self._path, key)
            try:
                if os.path.exists(path):
                    with open(path) as fd:
                        return omemo.storage.Just(json.load(fd))
                else:
                    return omemo.storage.Nothing()
            except Exception:
                raise omemo.storage.StorageException()

        async def _store(self, key, value):
            key = os.path.relpath(os.path.normpath(os.path.join("/", key)), "/")
            path = os.path.join(self._path, key)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fd:
                    json.dump(value, fd)
            except Exception:
                raise omemo.storage.StorageException()

        async def _delete(self, key):
            key = os.path.relpath(os.path.normpath(os.path.join("/", key)), "/")
            path = os.path.join(self._path, key)
            if os.path.exists(path):
                os.unlink(path)


slixmpp.plugins.register_plugin(XEP_0384)
