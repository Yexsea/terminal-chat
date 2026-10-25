"""Yakishi Terminal Chat — relay server.

The relay only forwards encrypted packets between the members of one room.
It never sees passwords, keys, names or message text.
"""
import asyncio
import json
import os
import re
import time
from http import HTTPStatus

from websockets.asyncio.server import broadcast, serve
from websockets.exceptions import ConnectionClosed

# Render port atarsa onu kullan, yoksa 10000
PORT = int(os.environ.get("PORT", 10000))
MAX_PACKET = 64 * 1024                    # bir paket en fazla 64 KB
MAX_PER_SECOND = 10                       # bağlantı başına saniyede en fazla 10 paket
ROOM_ID = re.compile(r"^[0-9a-f]{64}$")  # oda kimliği: istemcinin türettiği 32 baytlık hex

rooms = {}                                # oda kimliği -> bağlantılar


def health_check(connection, request):
    # Barındırma platformları (Render) servisi düz bir HTTP isteğiyle yoklar.
    # WebSocket olmayan isteklere "ayaktayım" cevabı verilir.
    if request.headers.get("Upgrade", "").lower() != "websocket":
        return connection.respond(HTTPStatus.OK, "Yakishi Relay Alive\n")
    return None


def leave(ws, room):
    members = rooms.get(room)
    if members:
        members.discard(ws)
        if not members:
            del rooms[room]


async def handler(ws):
    room = None
    window, count = time.monotonic(), 0
    try:
        async for raw in ws:
            # Hız sınırı
            now = time.monotonic()
            if now - window >= 1:
                window, count = now, 0
            count += 1
            if count > MAX_PER_SECOND or not isinstance(raw, str):
                continue

            try:
                packet = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(packet, dict):
                continue

            kind = packet.get("type")
            if kind == "join":
                new_room = packet.get("room")
                if isinstance(new_room, str) and ROOM_ID.match(new_room):
                    if room:
                        leave(ws, room)
                    room = new_room
                    rooms.setdefault(room, set()).add(ws)

            elif kind == "msg" and room:
                payload = packet.get("payload")
                if isinstance(payload, str) and 0 < len(payload) <= MAX_PACKET - 1024:
                    peers = [peer for peer in rooms.get(room, ()) if peer is not ws]
                    broadcast(peers, json.dumps({"type": "msg", "payload": payload}))
            # Diğer her şey (bozuk paketler dahil) yok sayılır, kimseye iletilmez.
    except ConnectionClosed:
        pass
    finally:
        if room:
            leave(ws, room)


async def main():
    async with serve(handler, "0.0.0.0", PORT, process_request=health_check,
                     max_size=MAX_PACKET, ping_interval=25, ping_timeout=20) as server:
        print(f"[*] Yakishi Relay Server {PORT} portunda dinliyor...")
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
