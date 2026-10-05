"""Boot script for the akgentic-infra community-tier server."""

from __future__ import annotations

import argparse

import logfire
import uvicorn
from akgentic.infra.server.app import create_app
from akgentic.infra.server.settings import CommunitySettings
from akgentic.infra.wiring import wire_community
from dotenv import load_dotenv

# Loaded at import time: CommunitySettings() reads the environment the moment it
# is constructed, so every AKGENTIC_* setting in .env must be in place before then.
# Variables already exported in the shell win over the file.
load_dotenv()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--logfire",
        action="store_true",
        help="Enable Logfire instrumentation.",
    )
    args = parser.parse_args()

    settings = CommunitySettings()
    services = wire_community(settings)
    app = create_app(services, settings)

    if args.logfire:
        logfire.configure(console=False)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ws="wsproto",
        timeout_graceful_shutdown=1,
    )
