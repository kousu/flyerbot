import argparse
import asyncio
import os
import logging

from .bot import FlyerBot

log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,  # bots are backend, so info is a good default level
    format="%(asctime)s %(levelname)-6s %(name)+25s:%(lineno)d: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

parser = argparse.ArgumentParser(
    description="""XMPP bot that OCRs flyer images and returns .ics files


    Provide XMPP credentials in
    - XMPP_JID
    - XMPP_PASSWORD

    Provide a platform.claude.com token in
    - ANTHROPIC_API_KEY

    """,
)
parser.add_argument(
    "--username",
    "-u",
    dest="jid",
    default=os.environ.get("XMPP_JID"),
    help="XMPP JID (default: $XMPP_JID)",
)
parser.add_argument(
    "--host", default=None, help="XMPP server hostname (default: extract from JID)"
)
parser.add_argument(
    "--port", type=int, default=5222, help="XMPP server port (default: 5222)"
)
parser.add_argument(
    "-v", "--verbose", action="count", default=0, help="Enable verbose logging"
)


async def amain():
    args = parser.parse_args()

    if args.verbose > 0:
        logging.getLogger().setLevel(logging.INFO)
    if args.verbose > 1:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.jid:
        parser.error("JID is required (use --username or set $XMPP_JID)")

    # Check for ANTHROPIC_API_KEY
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_key:
        parser.error("ANTHROPIC_API_KEY environment variable must be set")
        # TODO: pass this down into the library explicitly
        raise SystemExit(1)

    password = os.environ.get("XMPP_PASSWORD", "")

    # Create and configure the bot
    bot = FlyerBot(args.jid, password)

    # Configure server host/port if provided
    if args.host:
        bot["socket"].connect_to_host = (args.host, args.port)

    # Run the bot
    log.info(f"Connecting to XMPP as {args.jid}...")
    bot.connect()

    try:
        bot.connect()
        login = asyncio.create_task(bot.wait_until("session_start", timeout=30))

        async def connection_failed(event):
           print(f"Unable to connect to {bot.boundjid.bare}'s server.")
           login.cancel()
        async def failed_auth(event):
           print(f"Unable to login as '{bot.boundjid.bare}'. Check your password.", flush=True)
           login.cancel()

        bot.add_event_handler('connection_failed', connection_failed)
        bot.add_event_handler('failed_all_auth', failed_auth)

        await login
        print(f"Logged in as '{bot.boundjid.bare}'")

        # *once* connected, block until the but shuts down
        await bot.disconnected
    except TimeoutError as exc:
        raise SystemExit(1) from exc
    except asyncio.exceptions.CancelledError:
        await bot.disconnect()
    finally:
        # cancel then wait on any lingering threads
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
