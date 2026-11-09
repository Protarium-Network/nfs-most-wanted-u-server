"""UDP relay check without the game: two fake players swap packets through RelayHub ports.

Run: python test_relay.py   (uses UDP 3790-3793 on 127.0.0.1, so it can run next to a live server)
"""
import asyncio
import os
import socket

os.environ["NFSMWU_RELAY"] = "on"
os.environ["NFSMWU_RELAY_PORTS"] = "3790-3793"
import server  # noqa: E402


async def main():
    loop = asyncio.get_running_loop()
    hub = server.RELAY
    for port in hub.ports:
        await loop.create_datagram_endpoint(lambda p=port: server._RelayPort(p), local_addr=("127.0.0.1", port))
    a_to_b = hub.port_for(1, 2)   # player 2's address as told to player 1
    b_to_a = hub.port_for(2, 1)   # player 1's address as told to player 2

    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); a.bind(("127.0.0.1", 0)); a.setblocking(False)
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); b.bind(("127.0.0.1", 0)); b.setblocking(False)

    async def recv(sock):
        return await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), 2)

    b.sendto(b"hello from B (opens its endpoint)", ("127.0.0.1", b_to_a))   # A unknown yet: dropped
    await asyncio.sleep(0.2)
    a.sendto(b"race packet A->B", ("127.0.0.1", a_to_b))
    data, frm = await recv(b)
    assert data == b"race packet A->B", data
    assert frm[1] == b_to_a, f"B must see A at the port it was given ({b_to_a}), got {frm}"
    b.sendto(b"race packet B->A", ("127.0.0.1", b_to_a))
    data, frm = await recv(a)
    assert data == b"race packet B->A", data
    assert frm[1] == a_to_b, f"A must see B at the port it was given ({a_to_b}), got {frm}"
    for i in range(200):
        a.sendto(b"x" * 600, ("127.0.0.1", a_to_b))
        await recv(b)
    print(f"relay OK: A sees B at {a_to_b}, B sees A at {b_to_a}, {hub.packets} packets relayed")
    hub.release(1)
    assert not hub.pairs and len(hub.free) == len(hub.ports)
    print("release OK: ports returned")


asyncio.run(main())
