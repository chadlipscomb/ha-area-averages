#!/usr/bin/env python3
"""Minimal HA websocket client: reads a JSON list of messages on stdin,
sends them in order (auto-assigning ids), prints the JSON list of results.

Usage:
  echo '[{"type": "config/label_registry/list"}]' | python3 ha_ws.py

Needs the `websockets` package (any venv is fine); the HA token is read
from ~/.config/ha-token.
"""

import asyncio
import json
import pathlib
import ssl
import sys

import websockets

URL = "wss://homeassistant.local:8123/api/websocket"
TOKEN = pathlib.Path("~/.config/ha-token").expanduser().read_text().strip()


async def main() -> None:
    commands = json.load(sys.stdin)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    results = []
    async with websockets.connect(URL, ssl=ctx, max_size=2**24) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise SystemExit(f"auth failed: {reply}")
        for i, command in enumerate(commands, start=1):
            command["id"] = i
            await ws.send(json.dumps(command))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == i and message.get("type") == "result":
                    results.append(message)
                    break
    print(json.dumps(results, indent=2))


asyncio.run(main())
