"""LAN discovery: the coordinator broadcasts a small UDP beacon; workers listen.
The beacon is only a hint (spoofable) - trust comes from pairing code + human fingerprint check."""
import asyncio
import ipaddress
import json
import socket

import psutil

PORT = 7862


def broadcast_addrs():
    out = {"255.255.255.255"}
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and a.netmask and not a.address.startswith("127."):
                try:
                    out.add(str(ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False).broadcast_address))
                except ValueError:
                    pass
    return out


async def beacon(payload, interval=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setblocking(False)
    data = json.dumps(payload).encode()
    try:
        while True:
            for b in broadcast_addrs():
                try:
                    s.sendto(data, (b, PORT))
                except OSError:
                    pass
            await asyncio.sleep(interval)
    finally:
        s.close()


async def listen(seconds):
    """Return {ip: beacon} heard within `seconds`."""
    found = {}

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            try:
                j = json.loads(data[:2048])
                if j.get("meshram") == 1 and isinstance(j.get("port"), int):
                    found[addr[0]] = j
            except ValueError:
                pass

    loop = asyncio.get_running_loop()
    tr, _ = await loop.create_datagram_endpoint(P, local_addr=("0.0.0.0", PORT), allow_broadcast=True)
    try:
        await asyncio.sleep(seconds)
    finally:
        tr.close()
    return found
