# Flyer OCR Bot 🐗

An XMPP bot that converts flyers to importable and shareable .ics calendar events.

## Installation

```bash
pip install git+https://github.com/kousu/flyerbot
```

or

```
uv tool install git+https://github.com/kousu/flyerbot
```

## Usage

### Environment Variables

You need an XMPP account for the bot, and a Claude-API account for the OCR. The API is relatively expensive and charged separately from Claude Code; we are working on trying to run ML locally for savings and privacy.

- `XMPP_OCR_JID` - Your XMPP JID (optional, can also be passed via `--username`)
- `XMPP_OCR_PASSWORD` - Your XMPP password (optional, will prompt if not set)
- `ANTHROPIC_API_KEY` - An API token from a [Claude API account](https://platform.claude.com/)

Or set environment variables:

```bash
export XMPP_OCR_JID=your-jid@example.com
export XMPP_OCR_PASSWORD=your-jid@example.com
export ANTHROPIC_API_KEY=your-api-key
```

And launch it:

```
flyerbot
```

The bot only responds to people on it's roster/in group chats it's in, so to use
it you need to first log in as it and add some friends and join some group chats
from a normal client. Then:

1. **Send an image** to the bot/to one of the groups it's in.
2. **The bot responds** with an .ics file containing the details of the event in the image, if any are found.

### FAST Token Authentication

The bot [caches XEP-0484 (FAST) tokens](https://xmpp.org/extensions/xep-0484.html). If enabled on your server, this means you only need to log in with a password once manually and after the token should allow noninteractive use.

To log out, currently you have to

```
rm -r ~/.local/share/flyerbot/${XMPP_OCR_JID}/fast-token.json
```

## Deployment

To deploy 'in production' see [contrib/deploy.md](contrib/deploy.md).

## TODO

- [ ] Split slixmpp_sasl2 to a separate library
- [ ] Split slixmpp_fast to a separate library
- [ ] usage limits
  - [ ] rate-limits per user ?
- [ ] telegram, discord, etc versions
  - or maybe better to run this + matterbridge 🤷

## Development

```
git clone https://github.com/kousu/flyerbot
cd flyerbot

python -m venv .venv .; .venv/bin/activate; pip install -e .
# **or**
uv pip install -e .
````

> [!NOTE]
> This depends on and is being developed in tandem with https://githu.com/kousu/icsmtl/; if you need to use a specific branch or work on that locally:

```
(cd ..; git clone https://github.com/kousu/icsmtl)

. .venv/bin/activate; pip install -e . ../icsmtl
# **or**
uv pip install -e ../icsmtl
```

If you use `uv`, add this to pyproject.toml:

```
[tool.uv.sources]
icsmtl = { editable=true, path="../icsmtl" }
```

And run `uv sync`

You can test with `uv run flyerbot`.
