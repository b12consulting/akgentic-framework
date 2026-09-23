"""Community-tier akgentic server with the Telegram channel wired in.

Wires the Telegram channel into the community services, opens a cloudflared
*quick tunnel* (no Cloudflare account needed), and points the bot's webhook at
it for the lifetime of the process. The webhook is removed and the tunnel torn
down on exit. The bot token is read from ``TELEGRAM_BOT_TOKEN`` in ``.env``.

Each Telegram chat is bound to at most one team, as one of that team's agents.
The chat speaks *as* the bound agent and receives what that agent is sent.

Starting a team
    ``/new <text>``, or any message in an unbound chat, creates a team from
    ``--telegram-catalog``, binds the chat to its entry point (``@Human``), and
    sends the text as that agent to its supervisor (``@Manager``). The bot
    answers with the team id and the bound agent. ``/new`` in a bound chat
    releases the old team first; the old team keeps running.

Talking to a bound team
    A message is sent as the bound agent to the team's supervisor — the first
    supervisor that is not the bound agent itself, usually ``@Manager``. To
    address someone else, either:

    - start the message with ``@agent_name`` (a name later in the text is part
      of the sentence and does not route), or
    - reply to a bot message that names an agent; the first ``@agent_name`` in
      it is the recipient. A leading ``@agent_name`` wins over the reply.

Commands
    ``/status``
        Show the bound team, the bound agent, and the team's state.
    ``/unregister``
        Release the binding. The team is not stopped.
    ``/register <team-id> @agent_name``
        Bind the chat to an existing team as that agent; later messages are
        sent as it, and the new binding replaces whatever the chat was bound
        to before.

        The two values are read *independently*: each is looked for first in
        the command's own text, then in the message being replied to. So the
        command carries whichever half the reply does not — all three of these
        bind:

        - ``/register 08f5c5ef-… @Support`` — both typed, no reply needed;
        - ``/register @Support`` in reply to a "Started a new session — team
          <id>" notice: the notice gives the id, the command gives the agent;
        - ``/register`` in reply to a message that names both.

        If either is still missing after both texts, nothing is bound and the
        bot answers with the usage line. Neither value is verified, so a
        mistyped id or agent binds just as successfully — and only shows up
        later, as a chat whose messages are never answered.

Replies reach the chat only when the bound agent is a human seat (a
``UserProxy`` such as ``@Human`` or ``@Support``). A chat bound to an ordinary
member such as ``@Expert`` can still send as it, but receives nothing back.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import logfire
import uvicorn
from akgentic.infra.adapters.shared import ChannelConfig
from akgentic.infra.adapters.shared.owner_or_admin_policy import OwnerOrAdminPolicy
from akgentic.infra.protocols.authz import TeamAccessContext, TeamListFilter
from akgentic.infra.server.app import create_app
from akgentic.infra.server.auth import RequestUser
from akgentic.infra.server.settings import CommunitySettings
from akgentic.infra.wiring import wire_community
from dotenv import load_dotenv

# Loaded at import time, NOT inside a helper: CommunitySettings() reads the
# environment the moment it is constructed, so a .env loaded later is invisible
# to every AKGENTIC_* setting — the token would resolve and nothing else would.
load_dotenv()

logger = logging.getLogger("infra_server")

TELEGRAM_API = "https://api.telegram.org"
TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"

# cloudflared prints the quick-tunnel hostname inside a boxed banner on stderr.
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
TUNNEL_READY_TIMEOUT_S = 40.0
TUNNEL_REACHABLE_TIMEOUT_S = 120.0
TUNNEL_DNS_RESOLVER = "1.1.1.1"
TUNNEL_BLIND_GRACE_S = 25.0

DEFAULT_CHANNEL_REGISTRY_PATH = Path("data/channel_registry.yaml")

# Advertised in the chat's "/" menu. Telegram still delivers each one as an
# ordinary `message` update whose text starts with the slash — registering them
# buys the autocomplete affordance, not any dispatch.
BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "new", "description": "Send a message to a new team"},
    {"command": "status", "description": "Status of the current binding"},
    {"command": "register", "description": "Bind this chat: /register team_id @Agent"},
    {"command": "unregister", "description": "Unregister the current team"},
]

# The parser understands `message` only; every other update type (my_chat_member
# on Start, edited_message, ...) has no "message" key and would 400.
ALLOWED_UPDATES: list[str] = ["message"]

# One delivery at a time — see the setWebhook call for why.
MAX_CONNECTIONS = 1


def _require_token() -> str:
    """Return the bot token from the environment, failing loud when unset."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        msg = (
            f"{TOKEN_ENV_VAR} is not set. Add it to .env "
            f'(e.g. export {TOKEN_ENV_VAR}="7123456789:AA...") — get one from @BotFather.'
        )
        raise SystemExit(msg)
    return token


def _telegram_call(token: str, method: str, **params: object) -> dict[str, object]:
    """Call one Bot API method, raising with Telegram's own description on failure.

    ``raise_for_status`` is deliberately not used: Telegram answers a bad token
    or a rejected URL with a JSON ``description`` that is the only actionable
    part of the response, and the status alone discards it.
    """
    response = httpx.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params, timeout=15.0)
    body: dict[str, object] = response.json()
    if not body.get("ok"):
        msg = f"Telegram {method} failed: {body.get('description', response.text)}"
        raise RuntimeError(msg)
    return body


def _start_tunnel(port: int) -> tuple[subprocess.Popen[str], str]:
    """Start a cloudflared quick tunnel to ``port`` and return it with its public URL.

    The reader thread runs for the process lifetime rather than only until the
    URL is found: cloudflared keeps writing to stderr, and an unread pipe
    eventually fills and blocks the tunnel.
    """
    proc = subprocess.Popen(  # noqa: S603
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    found: list[str] = []
    ready = threading.Event()

    def _drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if not ready.is_set():
                match = TUNNEL_URL_RE.search(line)
                if match:
                    found.append(match.group(0))
                    ready.set()
            logger.debug("cloudflared: %s", line.rstrip())

    threading.Thread(target=_drain, daemon=True).start()

    if not ready.wait(TUNNEL_READY_TIMEOUT_S):
        proc.terminate()
        msg = (
            f"cloudflared did not publish a tunnel URL within {TUNNEL_READY_TIMEOUT_S:.0f}s. "
            "Re-run with --log-level debug to see its output."
        )
        raise SystemExit(msg)
    return proc, found[0]


def _await_tunnel(public_url: str) -> None:
    """Block until the quick-tunnel hostname is published in public DNS.

    A quick tunnel gets its own record — ``*.trycloudflare.com`` is not a
    wildcard — published a few seconds AFTER cloudflared prints the URL.
    Looking the name up inside that window is actively harmful: the SOA sets a
    1800s negative TTL, so one premature query makes the local resolver serve
    NXDOMAIN for the next half hour even though the record is live everywhere
    else. So the probe deliberately asks a public resolver and never touches
    the local one.

    Telegram resolves the host with its own resolver, so what this waits for
    is publication upstream, not local reachability.
    """
    host = urlsplit(public_url).hostname
    if host is None:
        msg = f"Cannot read a hostname out of {public_url!r}"
        raise SystemExit(msg)

    if shutil.which("dig") is None:
        logger.warning(
            "dig not found — waiting %.0fs blind before registering the webhook",
            TUNNEL_BLIND_GRACE_S,
        )
        time.sleep(TUNNEL_BLIND_GRACE_S)
        return

    deadline = time.monotonic() + TUNNEL_REACHABLE_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(  # noqa: S603
                [shutil.which("dig") or "dig", "+short", f"@{TUNNEL_DNS_RESOLVER}", host, "A"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.SubprocessError:
            # 1.1.1.1 unreachable on this network — fall back to waiting blind
            # rather than querying the local resolver and poisoning it.
            logger.warning("DNS probe failed; waiting %.0fs blind", TUNNEL_BLIND_GRACE_S)
            time.sleep(TUNNEL_BLIND_GRACE_S)
            return
        if probe.stdout.strip():
            logger.info("%s is live in public DNS", host)
            return
        time.sleep(2.0)

    msg = (
        f"{host} was not published in DNS within {TUNNEL_REACHABLE_TIMEOUT_S:.0f}s. "
        "The tunnel is up but Cloudflare never advertised the name."
    )
    raise SystemExit(msg)


class _SingleOperatorPolicy(OwnerOrAdminPolicy):
    """Treat every team as the local operator's, whoever the store says owns it.

    A channel-created team is owned by the chat id (``8892740599``), while the
    web UI's principal is ``anonymous``, so under the owner-filtered default the
    teams this server exists to demonstrate are invisible.

    ``AKGENTIC_ADMIN_LIST_ALL_TEAMS`` alone cannot fix that on the community
    tier. It gates on ``"admin" in user.roles``, and community mounts no
    ``RequireAuthMiddleware`` — so ``get_request_user`` never consults the wired
    ``AuthStrategy`` at all and falls back to a hardcoded roleless anonymous
    principal. Supplying a role-carrying strategy is dead code here; the policy
    is the seam that actually runs.

    **Both methods are overridden, and the second is not optional.** Widening
    only ``list_filters`` produces a worse state than not widening at all: the
    teams appear in the sidebar and every click answers 404, because
    ``can_get_team`` and its four siblings all delegate to ``is_allowed``. A
    list you cannot open is not a half-fix, it is a bug.

    The cost is stated plainly: this principal can read, stop, delete, restore
    and re-tag **every** team on the box, including ones a channel created for
    somebody else. That is the right trade for a single-operator local demo and
    wrong for anything two people can reach.
    """

    async def list_filters(self, *, user: RequestUser) -> list[TeamListFilter]:
        del user
        return [TeamListFilter()]

    async def is_allowed(self, *, ctx: TeamAccessContext, user: RequestUser) -> bool:
        del ctx, user
        return True


def _build_settings(token: str, catalog_namespace: str) -> CommunitySettings:
    """Community settings carrying the Telegram channel and a live registry path.

    The channel MUST arrive through ``settings.channels``, not by assigning
    ``services.channel_parser_registry`` after wiring. ``wire_community`` builds
    the parser registry *and* the outbound ``InteractionChannelDispatcher`` from
    that one field, and the dispatcher captures ``get_adapters()`` at
    construction. Replacing the registry afterwards fixes the inbound path — the
    webhook route reads it off ``app.state`` — while the dispatcher keeps the
    empty adapter list it was born with, so the agents reply into the void and
    nothing in the log says why.

    Without a ``channel_registry_path`` the channel registry is a no-op and
    ``find_team`` always returns ``None``, so every inbound message would start
    a new team instead of continuing the existing one. An env-supplied path wins.
    """
    return CommunitySettings(
        channels={
            "telegram": ChannelConfig(
                parser_fqcn="akgentic.infra.adapters.shared.TelegramChannelParser",
                adapter_fqcn="akgentic.infra.adapters.shared.TelegramChannelAdapter",
                # ``allow_register`` turns on ``/register <team-id> @Agent``,
                # which binds this chat to the team and agent the message names,
                # each of the two also readable from a replied-to message.
                # Neither is verified, and the webhook is unauthenticated, so
                # anyone who can message the bot and knows a team id can bind to
                # it — fine for a local demo bot, not for a shared deployment.
                config={
                    "bot_token": token,
                    "allow_register": "true",
                    "default_catalog_entry": catalog_namespace,
                },
            )
        },
        channel_registry_path=DEFAULT_CHANNEL_REGISTRY_PATH,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--logfire",
        action="store_true",
        help="Enable Logfire instrumentation.",
    )
    parser.add_argument(
        "--telegram-catalog",
        default="agent-team",
        help="Catalog namespace new Telegram-initiated teams are created from.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO)

    token = _require_token()
    settings = _build_settings(token, args.telegram_catalog)

    tunnel, public_url = _start_tunnel(settings.port)
    webhook_url = f"{public_url}/webhook/telegram"
    logger.info("Tunnel open at %s — waiting for its DNS record", public_url)

    _await_tunnel(public_url)

    _telegram_call(
        token,
        "setWebhook",
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=ALLOWED_UPDATES,
        max_connections=MAX_CONNECTIONS,
    )
    logger.info("Telegram webhook registered: %s", webhook_url)

    _telegram_call(token, "setMyCommands", commands=BOT_COMMANDS)
    logger.info("Telegram commands registered: %s", [c["command"] for c in BOT_COMMANDS])

    services = wire_community(settings, team_access_policy=_SingleOperatorPolicy())
    logger.info("Outbound adapters wired: %d", len(services.channel_parser_registry.get_adapters()))
    app = create_app(services, settings)

    if args.logfire:
        logfire.configure(console=False)
        logfire.instrument_pydantic_ai()

    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            ws="wsproto",
            timeout_graceful_shutdown=1,
        )
    finally:
        if token:
            try:
                _telegram_call(token, "deleteWebhook")
                logger.info("Telegram webhook removed")
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("Could not remove Telegram webhook: %s", exc)
        if tunnel is not None:
            tunnel.terminate()


if __name__ == "__main__":
    main()
