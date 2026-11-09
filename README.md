# Need for Speed Most Wanted U - online server (Wii U)

A community Blaze server that brings online play back to **Need for Speed Most Wanted U** on Wii U
(EA Hawaii, BlazeSDK 3.19, update v32): free roam lobbies, Autolog (speedlists / speedwalls / stats), license plates
and a UDP relay for the race traffic.

It is the Wii U fork of a PC server, with one Protarium change: the player is identified by the **service token**
the console gets from the account server instead of a typed NNID.

## Credits

- **nfs13** (PC server, the original source) by the **sn0wd0wn** team: gregel, Dantiox, Alexes
  - https://github.com/178lavash178-create/nfs13 (see `AUTHORS`)
- **[Charliebear31](https://github.com/Charliebear31)** ported the server to the Wii U (plain TCP Blaze, `wiiULogin`,
  Wii U Autolog, relay) in the "NFSMWU Online Community" project this fork comes from.
- Protarium Network: service-token login (`get_for_token`), the console-side patch in Inkay, deployment.

Licensed under **AGPL-3.0** (see `LICENSE`). If you run a modified version for other people you must publish its source.
Not affiliated with EA, Criterion or Nintendo. You need your own copy of the game.

## Run

Python 3.11+, no dependencies.

```bash
NFSMWU_BIND=0.0.0.0 NFSMWU_PUBLIC_HOST=<public ip or dns name> python3 server.py
python3 -m unittest test_server test_relay
```

Ports: TCP 42127 (Blaze, plain), 44125, 17501 (Autolog HTTP), UDP 17502 (QoS) and 3660-3759 (race relay).

| Variable | Meaning |
|---|---|
| `NFSMWU_BIND` | listen address (default `127.0.0.1`) |
| `NFSMWU_PUBLIC_HOST` | address given to clients that are not on your LAN |
| `NFS13_HOST` | address given to LAN clients |
| `NFSMWU_STATE_DIR` | where player/stat files are stored |
| `NFSMWU_RELAY_PORTS` | relay port range (default `3660-3759`) |

`names.txt` in the state dir (optional): `<8 hex player id>=<display name>[,<principal id>]`.

## Client side

The game must talk plain TCP to this server and skip a login-flag check; this is done by the
[Inkay plugin](https://github.com/Protarium-Network/Inkay-GitHub-Release) (`patch_nfs_online`) or the Cemu graphic pack
of the original community project. Update v32 only (EUR and USA code is byte-identical).

## Security notes

There are **no passwords**: the server trusts the token/ID each console sends. Do not expose it as an authenticated
service. No secrets are stored in this repository.
