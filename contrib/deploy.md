# Deployment

This is one way to deploy this. There are variations, this is just what's worked for me.

## Install using `uv`

```
export UV_TOOL_DIR=/usr/local/lib
export UV_TOOL_BIN_DIR=/usr/local/bin
uv tool install git+https://github.com/kousu/flyerbot
```

> [!TIP]
> You should be able to use `pipx` similarly but I haven't worked out the exact incantation.

> [!TIP]
You could also `pip install --break-system-packages`; honestly it should be fine, I'm not pinning any dependencies crazy high.

As root:

```
useradd --system -m -s /bin/bash -b /var/lib flyerbot
su flyerbot -c 'mkdir -p ~flyerbot/.config/flyerbot'

cp contrib/credentials.sh ~flyerbot/.config/flyerbot
chmod 600 ~flyerbot/.config/flyerbot/credentials.sh
chown flyerbot ~flyerbot/.config/flyerbot/credentials.sh
chgrp flyerbot ~flyerbot/.config/flyerbot/credentials.sh
vi ~flyerbot/.config/flyerbot/credentials.sh # fill it in with an XMPP + platform.claude.ai account
```

## Service

As root:

```
cp contrib/flyerbot.service /etc/systemd/system/
systemctl enable --now flyerbot
```

Monitor with

```
journalctl -lfu flyerbot
```

## Authorization

To actually **use** the bot you need to grant permissions to it;
what that looks like for the bot is adding people/groups to its contacts.

Use a regular XMPP client to log in as the bot and add friends or join
MUCs with it. Anyone in those MUCs will be able to interact with it.
