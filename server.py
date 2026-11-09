# Copyright (C) 2026 sn0wd0wn (gregel, Dantiox, Alexes)
# SPDX-License-Identifier: AGPL-3.0-only
#
# Protarium fork of the NFSMWU Online Community server (itself the Wii U port, by Charliebear31, of the PC server nfs13
# by the sn0wd0wn team): a console-issued service token identifies the player (see get_for_token), state files live in
# NFSMWU_STATE_DIR. Modified 2026-11; see README.md for credits.
#
# Wii U fork (nfsmwu/server): Need for Speed Most Wanted U (Hawaii, BlazeSDK 3.19.04.0-cafe01).
# Changes from the PC server (github.com/178lavash178-create/nfs13): the Wii U client is patched (Cemu pack /
# Aroma plugin) to talk plain TCP, so Blaze (redirector + main server) listens directly on 42127 with no
# stunnel; redirector answers SECU 0; service name nfs-2013-wiiu; Authentication wiiULogin (Nintendo ticket,
# not checked) logs the player in. NFSMWU_BIND = listen address (127.0.0.1 default, 0.0.0.0 for a LAN console).
import time
import asyncio
import ipaddress
import os
import sys
import traceback
import struct
import logging
import secrets
import json
import random
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("blaze")

# Addresses the game clients use to reach this server. NFS13_HOST = the server's LAN IP (players on the same
# network, or 127.0.0.1 for Cemu on this PC); NFSMWU_PUBLIC_HOST = public IP or DNS name for everyone else
# (community server). Each connection is told the address that fits where it comes from (client_host()).
import contextvars
import socket
SERVER_HOST = os.environ.get("NFS13_HOST", "127.0.0.1")
PUBLIC_HOST = os.environ.get("NFSMWU_PUBLIC_HOST", "").strip()
BIND_HOST = os.environ.get("NFSMWU_BIND", "127.0.0.1")
STATE_DIR = Path(os.environ.get("NFSMWU_STATE_DIR", str(Path(__file__).resolve().parent)))
# names.txt of the token service (`<8 hex player id>=<name>[,<principal id>]`), optional
NAMES_FILE = Path(os.environ.get("NFSMWU_NAMES_FILE", ""))
GAME_SERVICE_NAME = "nfs-2013-wiiu"
_CLIENT_HOST = contextvars.ContextVar("client_host", default=SERVER_HOST)


def host_for_peer(ip: str) -> str:
    """Address to hand a client at `ip`: loopback -> 127.0.0.1, private LAN -> SERVER_HOST, internet -> PUBLIC_HOST."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return SERVER_HOST
    if a.is_loopback:
        return "127.0.0.1" if SERVER_HOST in ("127.0.0.1", "0.0.0.0") else SERVER_HOST
    if a.is_private or not PUBLIC_HOST:
        return SERVER_HOST
    return PUBLIC_HOST


def set_client_host(ip: str) -> str:
    host = host_for_peer(ip)
    _CLIENT_HOST.set(host)
    return host


def client_host() -> str:
    return _CLIENT_HOST.get()


def host_ipv4(host: str) -> int:
    """Numeric IPv4 of a host name or address (DNS names are resolved; 0 if that fails)."""
    try:
        return int(ipaddress.IPv4Address(host))
    except ValueError:
        try:
            return int(ipaddress.IPv4Address(socket.gethostbyname(host)))
        except OSError:
            return 0


def autolog_url() -> str:
    return f"http://{client_host()}:17501"


AUTOLOG_URL = f"http://{SERVER_HOST}:17501"  # kept for code that runs outside a client connection


# Latin only: the client's handling of other name encodings is untested.
PLAYER_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,15}")
# Nintendo Network ID: 6-16 letters, digits, '-', '_', '.'.
NNID_RE = re.compile(r"[A-Za-z0-9_.-]{6,16}")
# Protarium service token: 8 hex digits of player id + more hex
TOKEN_RE = re.compile(r"[0-9a-fA-F]{40,}")


@dataclass(frozen=True)
class PlayerProfile:
    persona_id: int
    account_id: int
    name: str
    client_ip: str


class PlayerRegistry:
    """Stable LAN-IP to Blaze persona mapping for real multi-client sessions."""

    def __init__(self, path=None):
        self.path = Path(path) if path else STATE_DIR / "players.json"
        self._profiles = {}
        self._ip_alias = {}
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Unable to load player registry %s: %s", self.path, exc)
            return
        entries = raw if isinstance(raw, list) else raw.get("players", [])
        for entry in entries:
            try:
                client_ip = str(entry["client_ip"])
                self._profiles[client_ip] = PlayerProfile(
                    int(entry["persona_id"]),
                    int(entry.get("account_id", entry["persona_id"])),
                    str(entry["name"]),
                    client_ip,
                )
            except (KeyError, TypeError, ValueError):
                log.warning("Ignoring malformed player registry entry: %r", entry)

    def _save(self):
        data = {
            "players": [
                {
                    "client_ip": profile.client_ip,
                    "persona_id": profile.persona_id,
                    "account_id": profile.account_id,
                    "name": profile.name,
                }
                for profile in sorted(
                    self._profiles.values(), key=lambda item: item.persona_id
                )
            ]
        }
        try:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            log.warning("Unable to save player registry %s: %s", self.path, exc)

    def get(self, client_ip: str) -> PlayerProfile:
        lookup_ip = client_ip
        if lookup_ip in self._ip_alias:
            return self._ip_alias[lookup_ip]
        profile = self._profiles.get(lookup_ip)
        if profile is not None:
            return profile

        used_ids = {item.persona_id for item in self._profiles.values()}
        persona_id = 1000001
        while persona_id in used_ids:
            persona_id += 1
        profile = PlayerProfile(
            persona_id, persona_id, f"Player{persona_id - 1000000}", lookup_ip
        )
        self._profiles[lookup_ip] = profile
        self._save()
        log.info("Registered new player %s id=%d for %s",
                 profile.name, profile.persona_id, lookup_ip)
        return profile

    def rename(self, client_ip: str, name: str) -> str:
        """Rename the profile of a known client; returns an error or ""."""
        profile = self._profiles.get(client_ip)
        if profile is None:
            return "unknown player: log in to the game first"
        if not PLAYER_NAME_RE.fullmatch(name):
            return "name must be 1-15 latin letters, digits, _ or -"
        if any(other.name.lower() == name.lower() and other is not profile
               for other in self._profiles.values()):
            return "name is already taken"
        self._profiles[client_ip] = replace(profile, name=name)
        self._save()
        log.info("Renamed player id=%d %s -> %s for %s",
                 profile.persona_id, profile.name, name, client_ip)
        return ""

    def get_for_nnid(self, nnid: str, client_ip: str) -> PlayerProfile:
        """Wii U: one profile per Nintendo Network ID, named after it (stored under key 'nnid:<id>').
        The client IP is mapped to it too (not saved) so Autolog HTTP lookups by IP find the same player."""
        key = "nnid:" + nnid.lower()
        profile = self._profiles.get(key)
        if profile is None:
            used_ids = {item.persona_id for item in self._profiles.values()}
            persona_id = 1000001
            while persona_id in used_ids:
                persona_id += 1
            profile = PlayerProfile(persona_id, persona_id, nnid, key)
            self._profiles[key] = profile
            self._save()
            log.info("Registered new player %s id=%d (NNID)", nnid, persona_id)
        self._ip_alias[client_ip] = profile
        return profile

    def get_for_token(self, token: str, client_ip: str) -> PlayerProfile:
        """Protarium: the Wii U's service token starts with the player id (8 hex digits, issued by the token service)."""
        tid = token[:8].lower()
        key = "tok:" + tid
        profile = self._profiles.get(key)
        if profile is None:
            name = "Racer" + tid[-6:].upper()
            try:
                for line in NAMES_FILE.read_text(encoding="utf-8").splitlines():
                    k, _, v = line.partition("=")
                    if k.strip().lower() == tid and v.strip():
                        name = v.split(",")[0].strip()[:15] or name
            except (OSError, ValueError):
                pass
            used_ids = {item.persona_id for item in self._profiles.values()}
            persona_id = 1000001
            while persona_id in used_ids:
                persona_id += 1
            profile = PlayerProfile(persona_id, persona_id, name, key)
            self._profiles[key] = profile
            self._save()
            log.info("Registered new player %s id=%d (token %s)", name, persona_id, tid)
        self._ip_alias[client_ip] = profile
        return profile

    def all_profiles(self):
        """Return one stable profile per Blaze id, in deterministic order."""
        unique = {profile.persona_id: profile
                  for profile in self._profiles.values()}
        return tuple(unique[persona_id] for persona_id in sorted(unique))


@dataclass
class ClientSession:
    writer: object
    transport_peer: tuple
    client_peer: tuple
    profile: PlayerProfile
    network_addr: bytes = None
    qdat: bytes = None
    hardware_flag: int = 0
    authenticated: bool = False
    game_id: int = 0
    matchmaking_id: int = 0
    friends_announced: bool = False
    game_list_id: int = 0
    game_list_subscription: bool = False
    wiiu_session_key: str = ""


@dataclass
class GameMember:
    session: ClientSession
    slot: int
    state: int = 2
    player_attrs: bytes = None
    custom_data: bytes = b""
    team_index: int = 0xFFFF


@dataclass
class GameRecord:
    game_id: int
    host_id: int
    request_payload: bytes
    capacity: int
    settings: int
    version: str
    name: str
    state: int = 1
    matchmaking: bool = False
    setup_announced: bool = False
    attributes: bytes = None
    presence: int = 1
    team_capacity: int = 0
    xnet_nonce: bytes = b""
    xnet_session: bytes = b""
    admins: set = field(default_factory=set)
    banned_players: set = field(default_factory=set)
    team_ids: list = field(default_factory=list)
    members: dict = field(default_factory=dict)


PLAYER_REGISTRY = PlayerRegistry()
ACTIVE_SESSIONS = {}
ACTIVE_GAMES = {}
NEXT_GAME_ID = 1
NEXT_MATCHMAKING_ID = 1
NEXT_GAME_LIST_ID = 1

AUTH_COMMANDS = {
    0x000A: "createAccount",
    0x0028: "login",
    0x0032: "silentLogin",
    0x003C: "expressLogin",
    0x0046: "logout",
    0x0064: "listPersonas",
    0x006E: "loginPersona",
    0x0098: "originLogin",
    0x002F: "getPrivacyPolicyContent",
    0x0029: "acceptTos",
    0x00F1: "acceptLegalDocs",
    0x00F2: "getEmailOptInSettings",
    0x00F6: "getTermsOfServiceContent",
}

# Wii U auth commands follow ps3Login (0xC8) in the client's name table: wiiUCreateAccount 0xC9,
# wiiUAssociateAccount 0xCA, wiiULogin 0xCB (seen: TOKN = nn::act independent service token, PASS = '').
AUTH_COMMANDS.update({0x00C8: "ps3Login", 0x00C9: "wiiUCreateAccount",
                      0x00CA: "wiiUAssociateAccount", 0x00CB: "wiiULogin"})
WIIU_LOGIN_COMMAND = [0x00CB]

GAME_MANAGER_COMMANDS = {
    0x0001: "createGame",
    0x0002: "destroyGame",
    0x0003: "advanceGameState",
    0x0004: "setGameSettings",
    0x0005: "setPlayerCapacity",
    0x0006: "setPresenceMode",
    0x0007: "setGameAttributes",
    0x0008: "setPlayerAttributes",
    0x0009: "joinGame",
    0x000B: "removePlayer",
    0x000D: "startMatchmaking",
    0x000E: "cancelMatchmaking",
    0x000F: "finalizeGameCreation",
    0x0011: "listGames",
    0x0012: "setPlayerCustomData",
    0x0013: "replayGame",
    0x0014: "returnDedicatedServerToPool",
    0x0015: "joinGameByGroup",
    0x0016: "leaveGameByGroup",
    0x0017: "migrateGame",
    0x0018: "updateGameHostMigrationStatus",
    0x0019: "resetDedicatedServer",
    0x001A: "updateGameSession",
    0x001B: "banPlayer",
    0x001D: "updateMeshConnection",
    0x001F: "removePlayerFromBannedList",
    0x0020: "clearBannedList",
    0x0021: "getBannedList",
    0x0026: "addQueuedPlayerToGame",
    0x0027: "updateGameName",
    0x0028: "ejectHost",
    0x0064: "getGameListSnapshot",
    0x0065: "getGameListSubscription",
    0x0066: "destroyGameList",
    0x0067: "getFullGameData",
    0x0068: "getMatchmakingConfig",
    0x0069: "getGameDataFromId",
    0x006A: "addAdminPlayer",
    0x006B: "removeAdminPlayer",
    0x006C: "setPlayerTeam",
    0x006D: "changeGameTeamId",
    0x006E: "migrateAdminPlayer",
    0x006F: "getUserSetGameListSubscription",
    0x0070: "swapPlayersTeam",
}

# Diagnostic: Blaze TDF members are optional on the wire.  A terminator-only
# response lets us distinguish an RPC/framing issue from a malformed member.
MINIMAL_SILENT_LOGIN_RESPONSE = False

URL_CONFIG = {
    "autolog_host": "{AUTOLOG}",
    "autolog_host_secure": "{AUTOLOG}",
    # Endpoint values are path components, not absolute URLs.  The retail
    # client prefixes each one with autolog_host itself.
    "event": "event",
    "event_secure": "event",
    "post_photo": "post_photo",
    "post_photo_secure": "post_photo",
    "get_photo": "get_photo",
    "get_photo_secure": "get_photo",
    "profile_sync": "profile_sync",
    "profile_sync_secure": "profile_sync",
    "friend_upload": "friend_upload",
    "friend_upload_secure": "friend_upload",
    "view_model": "view_model",
    "view_model_secure": "view_model",
    "page_data": "page_data",
    "page_data_secure": "page_data",
    "layout_bundles": "layout_bundles",
    "layout_bundles_secure": "layout_bundles",
    "lua_bundles": "lua_bundles",
    "lua_bundles_secure": "lua_bundles",
    "connType": "disabled",
    "tosUrl": "{AUTOLOG}/tos",
    "privacyPolicyUrl": "{AUTOLOG}/privacy",
}

# The first post-manifest request is built by the retail client from URL-table
# slot 0x11 (friend_upload_cc).  Keep that route and the manifest route
# distinct without inflating FetchClientConfig enough to perturb startup.
for _endpoint in ("server_manifest", "friend_upload_cc"):
    _url = _endpoint
    URL_CONFIG[_endpoint] = _url
    URL_CONFIG[f"{_endpoint}_secure"] = _url

GAME_CONFIG = {
    "tos_buffer": "140000",
    "max_concours": "10",
    "gameName": GAME_SERVICE_NAME,
    "serverList": "{HOST}:42127",
}


def config_for_client(config: dict) -> dict:
    """URL_CONFIG / GAME_CONFIG with {AUTOLOG} / {HOST} filled in for the current connection."""
    return {k: v.replace("{AUTOLOG}", autolog_url()).replace("{HOST}", client_host()) for k, v in config.items()}

def hexdump(data, length=16):
    lines = []
    for i in range(0, len(data), length):
        chunk = data[i:i+length]
        hex_str = " ".join(f"{b:02x}" for b in chunk)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:04x}  {hex_str:<{length*3}}  |{ascii_str}|")
    return "\n".join(lines)

def tdf_varint(value):
    if value < 0:
        raise ValueError("Expected non-negative integer")
    first = value & 0x3F
    value >>= 6
    out = bytearray([ first | (0x80 if value else 0) ])
    while value:
        part = value & 0x7F
        value >>= 7
        out.append(part | (0x80 if value else 0))
    return bytes(out)

def tdf_string(s: str) -> bytes:
    b = s.encode('latin1')
    return tdf_varint(len(b) + 1) + b + b"\x00"

def make_qos_config_info() -> bytes:
    """Blaze 3 QosConfigInfo for the local UDP echo endpoint."""
    def ping_site_info() -> bytes:
        info = bytearray()
        info += encode_tag("PSA", 0x01) + tdf_string(client_host())
        info += encode_tag("PSP", 0x00) + tdf_varint(17502)
        info += encode_tag("SNA", 0x01) + tdf_string("local")
        info.append(0x00)
        return bytes(info)

    payload = bytearray()
    payload += encode_tag("BWPS", 0x03) + ping_site_info()
    payload += encode_tag("LNP", 0x00) + tdf_varint(4)
    payload += encode_tag("LTPS", 0x05)
    payload += b"\x01\x03"  # map key=String, value=QosPingSiteInfo struct
    payload += tdf_varint(1)
    payload += tdf_string("local")
    payload += ping_site_info()
    payload += encode_tag("SVID", 0x00) + tdf_varint(1)
    payload.append(0x00)
    return bytes(payload)


def make_preauth_cids(component_ids=(
        0x0001,  # Authentication
        0x0004,  # GameManager
        0x0006,  # Playgroups
        0x0007,  # Stats
        0x0009,  # Util
        0x000F,  # Messaging
        0x0015,  # Rooms
        0x0019,  # AssociationLists
        0x001C,  # GameReporting
        0x0801,  # NFS title component
        0x7802,  # UserSessions
)):
    payload = bytearray.fromhex("8E 99 33 04")
    payload.append(0x00) # List element type: Integer
    payload += tdf_varint(len(component_ids))
    for cid in component_ids:
        payload += tdf_varint(cid)
    payload += encode_tag("QOSS", 0x03)
    payload += make_qos_config_info()
    payload.append(0x00) # Struct terminator
    return bytes(payload)

def make_association_member(profile: PlayerProfile, time_added=0) -> bytes:
    member_id = bytearray()
    member_id += tdf_int_field("BLID", profile.persona_id)
    member_id += tdf_string_field("PNAM", profile.name)
    member_id += tdf_int_field("XREF", 0)
    member_id += tdf_int_field("XTYP", 0)
    member_id.append(0x00)

    member = bytearray(encode_tag("LMID", 0x03))
    member += member_id
    member += tdf_int_field("TIME", time_added)
    member.append(0x00)
    return bytes(member)


def make_association_update_notification(profile: PlayerProfile,
                                         update_type=1) -> bytes:
    """Blaze::Association::UpdateListWithMembersRequest for friendList."""
    update = bytearray(encode_tag("LMID", 0x03))
    update += make_association_member(profile)
    update += tdf_int_field("LUPT", update_type)
    update.append(0x00)

    payload = bytearray(encode_tag("BIDL", 0x04))
    payload.append(0x03)  # ListMemberInfoUpdate struct
    payload += tdf_varint(1)
    payload += update
    payload += encode_tag("LID", 0x03)
    payload += tdf_string_field("LNM", "friendList")
    payload += tdf_int_field("TYPE", 1)
    payload.append(0x00)
    payload.append(0x00)
    return bytes(payload)


def make_association_lists_response(request_payload: bytes,
                                    owner_id=1000001,
                                    members=()) -> bytes:
    # Blaze::Association::Lists has one member, LMAP, a list of ListMembers.
    # The startup request asks for list type 1 without a name.  The server must
    # resolve that identification to the canonical friendList description;
    # echoing the unnamed request leaves the client's initialization callback
    # pending even though the TDF packet itself decodes successfully.
    alst = encode_tag("ALST", 0x04)
    if (len(request_payload) < 9 or request_payload[:4] != alst or
            request_payload[4] != 0x03 or request_payload[5] != 0x01):
        log.warning("Unexpected Association::getLists request; returning empty LMAP")
        payload = bytearray(encode_tag("LMAP", 0x04))
        payload += b"\x03\x00"
        return bytes(payload)

    payload = bytearray(encode_tag("LMAP", 0x04))
    payload.append(0x03)  # LMAP element type: ListMembers struct
    payload += tdf_varint(1)
    payload += encode_tag("INFO", 0x03)
    payload += encode_tag("BOID", 0x09)
    # The list object is owned by the authenticated Blaze user.  The old
    # KME3 sample value (0x074B09C4) belonged to its hard-coded test account
    # and leaves NFS13 waiting for a list owned by our local user.
    payload += tdf_varint(0x19) + tdf_varint(1) + tdf_varint(owner_id)
    payload += encode_tag("FLGS", 0x00) + tdf_varint(4)
    payload += encode_tag("LID", 0x03)
    payload += encode_tag("LNM", 0x01) + tdf_string("friendList")
    payload += encode_tag("TYPE", 0x00) + tdf_varint(1)
    payload.append(0x00)  # end of LID
    payload += encode_tag("LMS", 0x00) + tdf_varint(200)
    payload += encode_tag("PRID", 0x00) + tdf_varint(0)
    payload.append(0x00)  # end of INFO
    # NFS13's generated ListMembers metadata includes MEML between INFO and
    # OFRC.  Even with no friends the vector itself must be present so the
    # AssociationList callback can finish constructing the requested list.
    payload += encode_tag("MEML", 0x04)
    payload.append(0x03)  # list element type: ListMemberInfo struct
    members = tuple(member for member in members
                    if member.persona_id != owner_id)
    payload += tdf_varint(len(members))
    for member in members:
        payload += make_association_member(member)
    payload += encode_tag("OFRC", 0x00) + tdf_varint(0)
    payload += encode_tag("TOCT", 0x00) + tdf_varint(len(members))
    payload.append(0x00)  # end of ListMembers element
    return bytes(payload)

def make_fetch_config_response(config: dict) -> bytes:
    payload = bytearray.fromhex("8E FB A6 05") # CONF, Map
    payload.append(0x01) # key type String
    payload.append(0x01) # val type String
    payload += tdf_varint(len(config))
    for k, v in config.items():
        payload += tdf_string(k)
        payload += tdf_string(v)
    payload.append(0x00) # Struct terminator
    return bytes(payload)

def make_post_auth_response(account_id=1000001) -> bytes:
    """Exact NFS13 PostAuthResponse with telemetry/ticker disabled locally."""
    def tdf_int(tag_hex: str, val: int) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x00" + tdf_varint(val)

    def tdf_str(tag_hex: str, val: str) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x01" + tdf_string(val)

    def tdf_struct(tag_hex: str, inner: bytes) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x03" + inner + b"\x00"

    telemetry = bytearray()
    telemetry += tdf_str("86 4c b3", "127.0.0.1") # ADRS
    telemetry += tdf_int("86 eb ee", 1)           # ANON
    telemetry += tdf_str("92 9c e1", "1")        # DISA
    telemetry += tdf_str("9a 9b 34", "")         # FILT
    telemetry += tdf_int("b2 f8 c0", 0)           # LOC
    telemetry += tdf_str("ba fb eb", "1")        # NOOK
    telemetry += tdf_int("c2 fc b4", 0)           # PORT
    telemetry += tdf_int("ce 4b 39", 60)          # SDLY
    telemetry += tdf_str("ce 5c f3", "LOCAL")    # SESS
    telemetry += tdf_str("ce b9 79", "LOCAL")    # SKEY
    telemetry += tdf_int("cf 08 f4", 0)           # SPCT
    telemetry += tdf_str("cf 4a 6d", "0")        # STIM

    ticker = bytearray()
    ticker += tdf_str("86 4c b3", "")             # ADRS
    ticker += tdf_int("c2 fc b4", 0)              # PORT
    ticker += tdf_str("ce b9 79", "")             # SKEY

    user_options = bytearray()
    user_options += tdf_int("d2 db f0", 0)         # TMOP
    user_options += tdf_int("d6 99 00", account_id) # UID

    payload = bytearray()
    payload += tdf_struct("c3 3c c0", b"")                 # PSS
    payload += tdf_struct("d2 5b 25", bytes(telemetry))     # TELE
    payload += tdf_struct("d2 98 eb", bytes(ticker))        # TICK
    payload += tdf_struct("d7 2b f0", bytes(user_options))  # UROP
    payload.append(0x00)
    return bytes(payload)

def parse_cfid(payload: bytes) -> str:
    idx = payload.find(b"\x8e\x6a\x64\x01")
    if idx != -1:
        p = idx + 4
        first = payload[p]
        p += 1
        str_len = first & 0x3f
        if first & 0x80:
            shift = 6
            while p < len(payload):
                b = payload[p]
                p += 1
                str_len |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
        s = payload[p : p + str_len].rstrip(b"\x00")
        return s.decode('latin1', errors='replace')
    return ""

def make_full_login_response(persona_name="Player", persona_id=1000001,
                             account_id=1000001, session_key=None):
    if session_key is None:
        session_key = secrets.token_hex(16)

    def tdf_int(tag_hex: str, val: int) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x00" + tdf_varint(val)

    def tdf_str(tag_hex: str, s: str) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x01" + tdf_string(s)

    def tdf_struct(tag_hex: str, inner: bytes) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x03" + inner + b"\x00"

    now = int(time.time())

    # Exact PersonaDetails schema embedded in NFS13.lab.exe:
    # DSNM (string), LAST (integer), PID (int64), PLAT (enum/integer).
    pdtl = bytearray()
    pdtl += tdf_str("93 3b ad", persona_name) # DSNM
    pdtl += tdf_int("b2 1c f4", now)          # LAST
    pdtl += tdf_int("c2 99 00", persona_id)   # PID
    pdtl += tdf_int("c2 c8 74", 0)            # PLAT = PC/default

    # SESS (SessionInfo)
    sess = bytearray()
    sess += tdf_int("8b 5a 64", account_id)   # BUID
    sess += tdf_int("9b 2c f4", 0)            # FRST: existing local persona
    sess += tdf_str("ae 5e 40", session_key)  # KEY
    sess += tdf_int("b2 cb e7", now)          # LLOG
    sess += tdf_str("b6 1a 6c", f"{persona_name.lower()}@lab.lan") # MAIL
    sess += tdf_struct("c2 4d 2c", bytes(pdtl)) # PDTL
    sess += tdf_int("d6 99 00", account_id)   # UID

    # Exact FullLoginResponse schema embedded in NFS13.lab.exe. Keep this
    # deliberately free of newer Blaze fields: this client decodes the
    # response before running the silentLogin -> expressLogin callback.
    response = bytearray()
    response += tdf_int("86 7d 70", 0)              # AGUP
    response += tdf_int("86 eb ee", 0)              # ANON
    response += tdf_int("bb 4b f3", 0)              # NTOS: already accepted
    response += tdf_str("c2 3d 2b", f"LOCAL-{session_key}") # PCTK
    response += tdf_struct("ce 5c f3", bytes(sess)) # SESS
    response += tdf_int("cf 08 6d", 1)              # SPAM
    response += tdf_int("d6 e9 32", 0)              # UNDR
    response.append(0x00)
    return bytes(response)

def make_filter_user_text_response(request: bytes) -> bytes:
    """filterForProfanity: FilterUserTextResponse with every text PASSED (0).

    The callback at 0x00A87C60 reads the first list element without checking
    it, so an empty response crashes the client.
    """
    texts = [find_tdf_string(body, "UTXT")
             for body in find_tdf_group_list(request, "TLST")]
    return (encode_tag("TLST", 0x04) + b"\x03" + tdf_varint(len(texts)) +
            b"".join(tdf_int_field("DIRT", 0) +
                     tdf_string_field("UTXT", text) + b"\x00"
                     for text in texts) +
            b"\x00")

def make_email_opt_in_settings_response() -> bytes:
    """Exact NFS13 response schema: EAMC and PMC integer flags."""
    response = bytearray()
    response += bytes.fromhex("96 1b 63 00") + tdf_varint(0) # EAMC
    response += bytes.fromhex("c2 d8 c0 00") + tdf_varint(0) # PMC
    response.append(0x00)
    return bytes(response)

def make_legal_doc_content_response(content: str) -> bytes:
    """Exact GetLegalDocContentResponse: LDVC, TCOL and TCOT."""
    encoded = content.encode("latin1")
    response = bytearray()
    response += bytes.fromhex("b2 4d a3 01") + tdf_string("1")       # LDVC
    response += bytes.fromhex("d2 3b ec 00") + tdf_varint(len(encoded)) # TCOL
    response += bytes.fromhex("d2 3b f4 01") + tdf_string(content)   # TCOT
    response.append(0x00)
    return bytes(response)

def make_login_response(persona_name="Player", persona_id=1000001,
                        account_id=1000001, session_key=None):
    """Authentication.login (0x28) has a different response contract."""
    if session_key is None:
        session_key = secrets.token_hex(16)

    def tdf_int(tag_hex: str, val: int) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x00" + tdf_varint(val)

    def tdf_str(tag_hex: str, s: str) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x01" + tdf_string(s)

    persona = bytearray()
    persona += tdf_str("93 3b ad", persona_name)   # DSNM
    persona += tdf_int("b2 1c f4", int(time.time())) # LAST
    persona += tdf_int("c2 99 00", persona_id)     # PID
    persona += tdf_int("e3 29 66", account_id)     # XREF
    persona += tdf_int("e3 4e 70", 0)              # XTYP
    persona.append(0x00)

    response = bytearray()
    response += tdf_str("c2 3d 2b", f"LOCAL-{session_key}") # PCTK
    response += bytes.fromhex("c2 cc f4 04")        # PLST, List
    response.append(0x03)                            # element type Struct
    response += tdf_varint(1)
    response += persona
    response += tdf_str("c3 2a 76", f"{autolog_url()}/privacy") # PRIV
    response += tdf_str("ce b9 79", session_key)   # SKEY
    response += tdf_int("cf 08 6d", 1)             # SPAM
    response += tdf_str("d2 8c f4", f"{client_host()}:17501") # THST
    response += tdf_str("d3 5c a9", "/tos")       # TURI
    response += tdf_int("d6 99 00", account_id)    # UID
    response.append(0x00)
    return bytes(response)

# TDF class ids registered by the Wii U client (nfsmwu/tools/tdf-registry.js -> build/tdf-registry.txt). The ids
# are the same on PC (its "collectable" report is 0x9bbe96e0 = Bounty::Report).
TDF_CLASS_IDS = {
    0x81003a57: "Bounty::Bounty", 0x9bbe96e0: "Bounty::Report",
    0x179bc90e: "Challenge::OnlineChallenge", 0xd9c5d1b6: "Challenge::Report",
    0x8485661b: "Challenge::OnlineMilestone", 0xa675fc82: "Challenge::MilestoneReport",
    0x7ceb14e1: "Challenge::PlayerMilestoneReport", 0x2483903a: "Concours::ConcoursUpdate",
    0x1ecfaca5: "Concours::ConcoursReport", 0xdd8d086b: "LicensePlate::LicensePlate",
    0x79ae7eca: "LicensePlate::Report", 0x6c540902: "Online::StatUpdates",
    0x97c37701: "Online::AccoladeUpdate", 0x920f936c: "Online::AccoladeReport",
    0x793fb696: "Online::LicenseUpdate", 0x738bd301: "Online::LicenseReport",
    0xc21a2842: "Online::DoubleSP", 0x74e8b016: "Online::DoubleSPReport",
    0x6ffcbf43: "Online::OnlineEventResults", 0xc4bd435a: "Online::MultiplayerReport",
    0x0c6a9692: "RoadRules::RoadRule", 0xfaf60f0a: "RoadRules::Report",
}


def _tdf_tag_name(b0, b1, b2):
    v = (b0 << 16) | (b1 << 8) | b2
    out = ""
    for shift in (18, 12, 6, 0):
        c = (v >> shift) & 0x3F
        if c:
            out += chr(c + 0x20)
    return out


def tdf_dump(data: bytes, limit=4000) -> str:
    """Human-readable TDF for the log: TAG=value, {group}, [list], <map>. Best effort, never raises."""
    pos = 0

    def varint():
        nonlocal pos
        b = data[pos]; pos += 1
        val = b & 0x3F; neg = b & 0x40; shift = 6
        while b & 0x80:
            b = data[pos]; pos += 1
            val |= (b & 0x7F) << shift; shift += 7
        return -val if neg else val

    def value(t):
        nonlocal pos
        if t == 0:
            return str(varint())
        if t == 1:
            n = varint(); s = data[pos:pos + n]; pos += n
            return repr(s.rstrip(b"\0").decode("latin1"))
        if t == 2:
            n = varint(); s = data[pos:pos + n]; pos += n
            return f"blob[{n}]" + (s[:24].hex() + ("..." if n > 24 else ""))
        if t == 3:
            if pos < len(data) and data[pos] == 2:
                pos += 1
            return "{" + fields(True) + "}"
        if t == 4:
            et = data[pos]; pos += 1; n = varint()
            return "[" + ", ".join(value(et) for _ in range(n)) + "]"
        if t == 5:
            kt = data[pos]; vt = data[pos + 1]; pos += 2; n = varint()
            return "<" + ", ".join(f"{value(kt)}: {value(vt)}" for _ in range(n)) + ">"
        if t == 6:
            k = data[pos]; pos += 1
            if k == 0x7F:
                return "union(unset)"
            return f"union{k}(" + fields(False, 1) + ")"
        if t == 7:
            # Blaze 3 variable TDF: present byte, varint tdf id (TDF_CLASS_IDS), one tagged value, 0 terminator
            present = data[pos]; pos += 1
            if not present:
                return "var(none)"
            tdf_id = varint() & 0xffffffff
            inner = fields(True)
            return f"var<{TDF_CLASS_IDS.get(tdf_id, hex(tdf_id))}>({inner})"
        if t == 8:
            return f"objtype({varint()},{varint()})"
        if t == 9:
            return f"objid({varint()},{varint()},{varint()})"
        if t == 0xA:
            v = struct.unpack(">f", data[pos:pos + 4])[0]; pos += 4
            return f"{v:g}"
        raise ValueError(f"type {t}")

    def fields(in_group, count=None):
        nonlocal pos
        parts = []
        while pos < len(data) and (count is None or len(parts) < count):
            if data[pos] == 0:  # end of group (a real tag never starts with 0)
                pos += 1
                if in_group:
                    break
                continue
            tag = _tdf_tag_name(data[pos], data[pos + 1], data[pos + 2]); t = data[pos + 3]; pos += 4
            parts.append(f"{tag}={value(t)}")
        return " ".join(parts)

    try:
        text = fields(False)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        text = f"(decode stopped at {pos}: {exc}) raw={data.hex()}"
    return text if len(text) <= limit else text[:limit] + "..."


def make_wiiu_login_response(persona_name="Player", persona_id=1000001,
                             account_id=1000001, session_key=None):
    """Authentication.wiiULogin: ConsoleLoginResponse (AGUP, NTOS, SESS, SPAM)."""
    if session_key is None:
        session_key = secrets.token_hex(16)
    now = int(time.time())
    pdtl = (tdf_string_field("DSNM", persona_name) + tdf_int_field("LAST", now) +
            tdf_int_field("PID", persona_id) + tdf_int_field("PLAT", 0))
    sess = (tdf_int_field("BUID", account_id) + tdf_int_field("FRST", 0) +
            tdf_string_field("KEY", session_key) + tdf_int_field("LLOG", now) +
            tdf_string_field("MAIL", f"{persona_name.lower()}@wiiu.lan") +
            tdf_group_field("PDTL", pdtl) + tdf_int_field("UID", account_id))
    return (tdf_int_field("AGUP", 0) + tdf_int_field("NTOS", 0) +
            tdf_group_field("SESS", sess) + tdf_int_field("SPAM", 1) + b"\x00")


def make_blaze_response(component, command, msg_id, payload=b"", error=0x0000):
    return struct.pack(
        ">6H",
        len(payload),
        component,
        command,
        error,
        0x1000, # MESSAGE_TYPE_RESPONSE
        msg_id
    ) + payload

def make_blaze_notification(component, notification, payload=b"", msg_id=0):
    return struct.pack(
        ">6H",
        len(payload),
        component,
        notification,
        0,
        0x2000, # MESSAGE_TYPE_NOTIFICATION
        msg_id,
    ) + payload

def make_user_added_notification(blaze_id=1000001, account_id=1000001,
                                 persona_name="Player"):
    """Exact NFS13 NotifyUserAdded/UserIdentification wire schema."""
    def tdf_int(tag_hex: str, val: int) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x00" + tdf_varint(val)

    def tdf_struct(tag_hex: str, inner: bytes) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x03" + inner + b"\x00"

    def tdf_str(tag_hex: str, value: str) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x01" + tdf_string(value)

    # Exact UserIdentification schema in this NFS13 build begins with AID,
    # ALOC, EXBB, EXID, ID and NAME.  Optional/default fields may be omitted,
    # but the account, Blaze id and persona name must describe the local user.
    user = bytearray()
    user += tdf_int("86 99 00", account_id) # AID
    user += tdf_int("a6 40 00", blaze_id)   # ID
    user += tdf_str("ba 1b 65", persona_name) # NAME

    payload = bytearray()
    payload += tdf_struct("96 48 74", b"")          # EDAT (empty defaults)
    payload += tdf_int("9a c9 f3", 3)                # FLGS: subscribed | online
    payload += tdf_struct("d7 39 72", bytes(user))   # USER
    payload.append(0x00)
    return bytes(payload)

def make_lookup_user_response(request: bytes) -> bytes:
    """UserSessions.lookupUser: UserData for the requested Blaze id (or name)."""
    want_id = find_tdf_int(request, "ID", 0)
    want_name = find_tdf_string(request, "NAME", "")
    profile = None
    for item in PLAYER_REGISTRY.all_profiles():
        if (want_id and item.persona_id == want_id) or (want_name and item.name.lower() == want_name.lower()):
            profile = item
            break
    other = ACTIVE_SESSIONS.get(profile.persona_id) if profile else None
    uid = profile.persona_id if profile else want_id
    name = profile.name if profile else want_name
    notif = make_user_extended_data_update_notification(
        other.network_addr if other else None, other.qdat if other else None,
        other.hardware_flag if other else 0, uid)
    ext = notif[4:len(notif) - len(tdf_int_field("USID", uid)) - 1]  # struct body incl. its 0 terminator
    user = (tdf_int_field("AID", profile.account_id if profile else uid) + tdf_int_field("ID", uid) +
            tdf_string_field("NAME", name))
    return (encode_tag("EDAT", 0x03) + ext + tdf_int_field("FLGS", 3 if other else 2) +
            tdf_group_field("USER", user) + b"\x00")


def make_user_authenticated_notification(blaze_id=1000001, account_id=1000001,
                                         persona_name="Player", session_key=""):
    """Exact NFS13 UserSessionLoginInfo schema (member table 0x010DDB70).

    0x00C04A20 builds the local user from BUID and DSNM.  Without them it is
    left with id 0 and no name, so 0x00C143C0 never sees its extended data.
    """
    now = int(time.time())
    return (tdf_int_field("BUID", blaze_id) +
            tdf_string_field("DSNM", persona_name) +
            tdf_int_field("FRST", 0) +
            tdf_string_field("KEY", session_key) +
            tdf_int_field("LAST", now) +
            tdf_int_field("LLOG", now) +
            tdf_string_field("MAIL", f"{persona_name.lower()}@lab.lan") +
            tdf_int_field("PID", blaze_id) +
            tdf_int_field("PLAT", 0) +
            tdf_int_field("UID", account_id) +
            b"\x00")

def parse_network_info(payload: bytes):
    """Return the ADDR field and NQOS renamed to QDAT from updateNetworkInfo.

    Requests carry no root terminator, so NQOS keeps its own.  Without it every
    later field nests inside the group: the client then reads no THST in
    NotifyGameSetup (0x00BBF3C0) and 0x00BC8630 never starts the network mesh.
    """
    addr = find_tdf_field(payload, "ADDR", 0x06)
    nqos = find_tdf_field(payload, "NQOS", 0x03)
    if nqos is None:
        return addr, None
    return addr, encode_tag("QDAT", 0x03) + nqos[4:]


def double_sp_seconds_left(now=None) -> int:
    """Seconds of Double SP left right now under NFSMWU_DOUBLE_SP (server PC's local clock):
    'weekend' (default) = Friday 18:00 until Monday 00:00, 'always', 'off', or a custom weekly window
    'fri18-mon00' (start day+hour, end day+hour; days mon..sun)."""
    import datetime
    mode = os.environ.get("NFSMWU_DOUBLE_SP", "weekend").strip().lower()
    if mode == "off":
        return 0
    if mode == "always":
        return 24 * 3600
    if mode == "weekend":
        mode = "fri18-mon00"
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    try:
        start, end = mode.split("-")
        s_day, s_hour = days.index(start[:3]), int(start[3:] or 0)
        e_day, e_hour = days.index(end[:3]), int(end[3:] or 0)
    except (ValueError, IndexError):
        log.warning("Bad NFSMWU_DOUBLE_SP %r, Double SP off", mode)
        return 0
    now = now or datetime.datetime.now()
    week_start = (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    for shift in (-7, 0):  # a window may have started last week (e.g. Friday -> Monday)
        begin = week_start + datetime.timedelta(days=s_day + shift, hours=s_hour)
        finish = week_start + datetime.timedelta(days=e_day + shift, hours=e_hour)
        if finish <= begin:
            finish += datetime.timedelta(days=7)
        if begin <= now < finish:
            return int((finish - now).total_seconds())
    return 0


def make_user_extended_data_update_notification(network_addr=None, qdat=None,
                                                hardware_flag=0,
                                                user_id=1000001):
    """Encode NFS13 notification 1: DATA(UserSessionExtendedData)."""
    def tdf_int(tag_hex: str, val: int) -> bytes:
        return bytes.fromhex(tag_hex) + b"\x00" + tdf_varint(val)

    payload = bytearray()
    # ADDR is an optional/union.  Before updateNetworkInfo it is explicitly
    # unset (0x7f); afterwards we echo the client's already-valid TDF field.
    payload += (network_addr if network_addr is not None
                else encode_tag("ADDR", 0x06) + b"\x7f")
    payload += encode_tag("BPS", 0x01) + tdf_string("ea-sjc")
    payload += encode_tag("CTY", 0x01) + tdf_string("")
    # CVAR is a variable TDF (wire type 7, 0x00BCFE30): zero presence byte = empty.
    # Any other type fails the whole decode, and the update is dropped silently.
    payload += encode_tag("CVAR", 0x07) + b"\x00"
    # Keys are (Stats component 7 << 16) | id, read by 0x006D56D7..0x006D58AE.
    # Grant Limited Edition DoubleSP (0x0075AFF0) cannot start until id 5 and
    # ids 8-11 exist.  Non-zero values mark each grant as already done; with 0
    # the node (0x00762650) waits forever for a bit this server never updates.
    # Wii U: id 4 = Double SP seconds left (read at 0x0254aafc), set by the server's real-clock schedule.
    data_map = ((0x70001, 0x409A), (0x70004, double_sp_seconds_left()), (0x70005, 1), (0x70008, 1),
                (0x70009, 1), (0x7000A, 1), (0x7000B, 1))
    payload += (encode_tag("DMAP", 0x05) + b"\x00\x00" +
                tdf_varint(len(data_map)) +
                b"".join(tdf_varint(key) + tdf_varint(value)
                         for key, value in data_map))
    payload += tdf_int("a3 79 a7", hardware_flag)     # HWFG
    payload += (encode_tag("PSLM", 0x04) + b"\x00" + tdf_varint(1) +
                tdf_varint(0x0FFF0FFF))
    if qdat is None:
        qdat = (encode_tag("QDAT", 0x03) +
                tdf_int("92 2c 33", 0) +             # DBPS
                tdf_int("ba 1d 34", 0) +             # NATT
                tdf_int("d6 2c 33", 0) + b"\x00")  # UBPS/end QDAT
    payload += qdat
    payload += tdf_int("d6 1d 34", 0)                 # UATT
    payload += (encode_tag("ULST", 0x04) + b"\x09" + tdf_varint(1) +
                tdf_varint(4) + tdf_varint(1) + tdf_varint(1))
    payload.append(0x00)  # end UserSessionExtendedData

    # The handler at 0x00C04480 looks the session up by USID and drops the
    # update when it finds none, so DATA alone never reaches the local user.
    return (encode_tag("DATA", 0x03) + bytes(payload) +
            tdf_int_field("USID", user_id) + b"\x00")

def encode_tag(tag_str, tdf_type):
    tag = tag_str.encode('ascii')
    out = bytearray(4)
    out[3] = tdf_type & 0xFF
    length = len(tag)
    if length > 0:
        out[0] |= (tag[0] & 0x40) << 1
        out[0] |= (tag[0] & 0x10) << 2
        out[0] |= (tag[0] & 0x0F) << 2
    if length > 1:
        out[0] |= (tag[1] & 0x40) >> 5
        out[0] |= (tag[1] & 0x10) >> 4
        out[1] |= (tag[1] & 0x0F) << 4
    if length > 2:
        out[1] |= (tag[2] & 0x40) >> 3
        out[1] |= (tag[2] & 0x10) >> 2
        out[1] |= (tag[2] & 0x0C) >> 2
        out[2] |= (tag[2] & 0x03) << 6
    if length > 3:
        out[2] |= (tag[3] & 0x40) >> 1
        out[2] |= (tag[3] & 0x10)
        out[2] |= (tag[3] & 0x0F)
    return bytes(out)

def encode_varint(val):
    if val < 64:
        return bytes([val])
    out = bytearray()
    out.append((val & 63) | 128)
    cur = val >> 6
    while cur >= 128:
        out.append((cur & 127) | 128)
        cur >>= 7
    out.append(cur)
    return bytes(out)

def encode_string(s):
    s_bytes = s.encode('utf-8') + b'\x00'
    return encode_varint(len(s_bytes)) + s_bytes


def tdf_int_field(tag: str, value: int) -> bytes:
    return encode_tag(tag, 0x00) + tdf_varint(value)


def tdf_string_field(tag: str, value: str) -> bytes:
    return encode_tag(tag, 0x01) + tdf_string(value)


def tdf_empty_list(tag: str, element_type: int) -> bytes:
    return encode_tag(tag, 0x04) + bytes((element_type,)) + tdf_varint(0)


def tdf_string_list(tag: str, values) -> bytes:
    payload = bytearray(encode_tag(tag, 0x04))
    payload.append(0x01)
    payload += tdf_varint(len(values))
    for value in values:
        payload += tdf_string(value)
    return bytes(payload)


def tdf_blob_field(tag: str, value=b"") -> bytes:
    return encode_tag(tag, 0x02) + tdf_varint(len(value)) + bytes(value)


def tdf_group_field(tag: str, body=b"") -> bytes:
    return encode_tag(tag, 0x03) + bytes(body) + b"\x00"


def tdf_int_list(tag: str, values) -> bytes:
    payload = bytearray(encode_tag(tag, 0x04))
    payload.append(0x00)
    payload += tdf_varint(len(values))
    for value in values:
        payload += tdf_varint(value)
    return bytes(payload)


def tdf_empty_map(tag: str, key_type: int, value_type: int) -> bytes:
    return (encode_tag(tag, 0x05) + bytes((key_type, value_type)) +
            tdf_varint(0))


def tdf_object_id_field(tag: str, component=0, entity_type=0,
                        entity_id=0) -> bytes:
    return (encode_tag(tag, 0x09) + tdf_varint(component) +
            tdf_varint(entity_type) + tdf_varint(entity_id))


def decode_tdf_varint(data: bytes, offset: int):
    """Decode the unsigned Heat2 integer format used by Blaze 3."""
    if offset >= len(data):
        raise ValueError("truncated TDF integer")
    first = data[offset]
    offset += 1
    value = first & 0x3F
    if not (first & 0x80):
        return value, offset
    shift = 6
    while offset < len(data):
        part = data[offset]
        offset += 1
        value |= (part & 0x7F) << shift
        if not (part & 0x80):
            return value, offset
        shift += 7
        if shift > 69:
            break
    raise ValueError("invalid TDF integer")


def _skip_tdf_value(data: bytes, offset: int, tdf_type: int) -> int:
    """Return the byte after one untagged Heat2 value.

    This is intentionally small, but covers every Blaze 3 type used by the
    NFS13 GameManager requests.  Keeping field boundaries lets us preserve
    the retail client's opaque attribute maps and address unions byte-for-byte.
    """
    if tdf_type in (0x00, 0x0A):  # Integer / TimeValue
        _, offset = decode_tdf_varint(data, offset)
        return offset
    if tdf_type in (0x01, 0x02):  # String / Blob
        length, offset = decode_tdf_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise ValueError("truncated TDF string/blob")
        return end
    if tdf_type == 0x03:  # Struct/group body
        while offset < len(data) and data[offset] != 0:
            offset = tdf_field_end(data, offset)
        if offset >= len(data):
            raise ValueError("unterminated TDF group")
        return offset + 1
    if tdf_type == 0x04:  # List
        if offset >= len(data):
            raise ValueError("truncated TDF list")
        element_type = data[offset]
        count, offset = decode_tdf_varint(data, offset + 1)
        for _ in range(count):
            # Blaze 3's NetworkAddress list has a long-standing Heat encoder
            # quirk: it advertises Group elements, then prefixes each group
            # body with the tagged-union discriminator (normally 2).
            if (element_type == 0x03 and offset < len(data) and
                    data[offset] in (0x01, 0x02, 0x03, 0x04, 0x7F)):
                offset += 1
            offset = _skip_tdf_value(data, offset, element_type)
        return offset
    if tdf_type == 0x05:  # Map
        if offset + 1 >= len(data):
            raise ValueError("truncated TDF map")
        key_type, value_type = data[offset], data[offset + 1]
        count, offset = decode_tdf_varint(data, offset + 2)
        for _ in range(count):
            offset = _skip_tdf_value(data, offset, key_type)
            offset = _skip_tdf_value(data, offset, value_type)
        return offset
    if tdf_type == 0x06:  # Tagged union
        if offset >= len(data):
            raise ValueError("truncated TDF union")
        active_member = data[offset]
        offset += 1
        if active_member == 0x7F:
            return offset
        return tdf_field_end(data, offset)  # normally VALU
    if tdf_type == 0x07:  # Variable: present byte; if set, varint tdf id + one tagged value + 0 terminator
        if offset >= len(data):
            raise ValueError("truncated TDF variable")
        if data[offset] == 0:
            return offset + 1
        _, offset = decode_tdf_varint(data, offset + 1)
        return _skip_tdf_value(data, offset, 0x03)
    if tdf_type == 0x08:  # ObjectType
        _, offset = decode_tdf_varint(data, offset)
        _, offset = decode_tdf_varint(data, offset)
        return offset
    if tdf_type == 0x09:  # ObjectId
        for _ in range(3):
            _, offset = decode_tdf_varint(data, offset)
        return offset
    if tdf_type == 0x0B:  # Float32
        end = offset + 4
        if end > len(data):
            raise ValueError("truncated TDF float")
        return end
    raise ValueError(f"unsupported TDF type 0x{tdf_type:02X}")


def tdf_field_end(data: bytes, offset: int) -> int:
    if offset + 4 > len(data) or data[offset] == 0:
        raise ValueError("truncated TDF field")
    return _skip_tdf_value(data, offset + 4, data[offset + 3])


def find_tdf_field(payload: bytes, tag: str, tdf_type=None):
    """Return a complete top-level tagged field, or None when absent."""
    offset = 0
    tag_prefix = encode_tag(tag, 0)[:3]
    try:
        while offset < len(payload) and payload[offset] != 0:
            end = tdf_field_end(payload, offset)
            if (payload[offset:offset + 3] == tag_prefix and
                    (tdf_type is None or payload[offset + 3] == tdf_type)):
                return payload[offset:end]
            offset = end
    except ValueError as exc:
        log.warning("Unable to scan TDF request for %s: %s", tag, exc)
    return None


def find_tdf_string(payload: bytes, tag: str, default=""):
    field = find_tdf_field(payload, tag, 0x01)
    if field is None:
        return default
    try:
        length, offset = decode_tdf_varint(field, 4)
        return field[offset:offset + max(0, length - 1)].decode(
            "latin1", errors="replace"
        )
    except ValueError:
        return default


def find_tdf_int(payload: bytes, tag: str, default=0):
    field = find_tdf_field(payload, tag, 0x00)
    if field is None:
        return default
    try:
        value, _ = decode_tdf_varint(field, 4)
        return value
    except ValueError:
        return default


def find_tdf_blob(payload: bytes, tag: str, default=b""):
    """Decode a top-level Heat2 blob without losing embedded zero bytes."""
    field = find_tdf_field(payload, tag, 0x02)
    if field is None:
        return default
    try:
        length, offset = decode_tdf_varint(field, 4)
        end = offset + length
        if end > len(field):
            return default
        return field[offset:end]
    except ValueError:
        return default


def find_tdf_int_list(payload: bytes, tag: str):
    """Decode a top-level Heat2 list of integer values."""
    field = find_tdf_field(payload, tag, 0x04)
    if field is None or len(field) < 6 or field[4] != 0x00:
        return ()
    try:
        count, offset = decode_tdf_varint(field, 5)
        values = []
        for _ in range(count):
            value, offset = decode_tdf_varint(field, offset)
            values.append(value)
        return tuple(values)
    except ValueError:
        return ()


def find_tdf_group_list(payload: bytes, tag: str):
    """Return the encoded bodies from a top-level list<Group> field."""
    field = find_tdf_field(payload, tag, 0x04)
    if field is None or len(field) < 6 or field[4] != 0x03:
        return ()
    try:
        count, offset = decode_tdf_varint(field, 5)
        groups = []
        for _ in range(count):
            start = offset
            offset = _skip_tdf_value(field, offset, 0x03)
            groups.append(field[start:offset])
        return tuple(groups)
    except ValueError:
        return ()


def replace_tdf_field(payload: bytes, replacement: bytes) -> bytes:
    """Replace one top-level field while preserving all opaque peer data."""
    if len(replacement) < 4:
        return payload
    tag_prefix = replacement[:3]
    output = bytearray()
    offset = 0
    try:
        while offset < len(payload) and payload[offset] != 0:
            end = tdf_field_end(payload, offset)
            if payload[offset:offset + 3] != tag_prefix:
                output += payload[offset:end]
            offset = end
    except ValueError as exc:
        log.warning("Unable to replace TDF field %s: %s",
                    tag_prefix.hex(" "), exc)
        return payload
    output += replacement
    output.append(0x00)
    return bytes(output)


def _fallback_network_address(tag="PNET", ip=0xC0A8011B, port=3659):
    pair = bytearray()
    pair += tdf_group_field(
        "EXIP", tdf_int_field("IP", ip) + tdf_int_field("PORT", port)
    )
    pair += tdf_group_field(
        "INIP", tdf_int_field("IP", ip) + tdf_int_field("PORT", port)
    )
    return (encode_tag(tag, 0x06) + b"\x02" +
            tdf_group_field("VALU", pair))


# ---- UDP relay ------------------------------------------------------------------------------------------------
# Race traffic is peer to peer (game UDP port 3659). For a community server that fails across home routers, so the
# server gives every player relay addresses instead of the other players' real ones: for each ordered pair
# (to, about) one server UDP port is "about's address, as told to `to`". A packet `to` sends to that port is sent
# on to `about` from the port (about, to), so `about` sees it come from the address it was given for `to`. Each
# side's real address is learned from its own packets (and its NAT opens because it sends to the server first).
RELAY_ENABLED = os.environ.get("NFSMWU_RELAY", "on" if PUBLIC_HOST else "off").strip().lower() in ("on", "1", "yes")
_RELAY_RANGE = os.environ.get("NFSMWU_RELAY_PORTS", "3660-3759")
_RELAY_TO = contextvars.ContextVar("relay_to", default=None)  # persona id of the player a payload is built for


class RelayHub:
    def __init__(self):
        lo, _, hi = _RELAY_RANGE.partition("-")
        self.ports = list(range(int(lo), int(hi or lo) + 1))
        self.free = list(self.ports)
        self.pairs = {}        # (to_id, about_id) -> port
        self.owner = {}        # port -> (to_id, about_id)
        self.transports = {}   # port -> DatagramTransport
        self.endpoints = {}    # persona id -> (ip, port) learned from that player's own packets
        self.packets = 0

    def port_for(self, to_id, about_id):
        key = (to_id, about_id)
        if key not in self.pairs:
            if not self.free:
                log.warning("[RELAY] out of relay ports (%s)", _RELAY_RANGE)
                return None
            port = self.free.pop(0)
            self.pairs[key] = port
            self.owner[port] = key
            log.info("[RELAY] port %d = player %d as seen by %d", port, about_id, to_id)
        return self.pairs[key]

    def release(self, persona_id):
        for key in [k for k in self.pairs if persona_id in k]:
            port = self.pairs.pop(key)
            self.owner.pop(port, None)
            self.free.append(port)
        self.endpoints.pop(persona_id, None)

    def fallback_endpoint(self, persona_id):
        """Before a player has sent anything: its Blaze address (public EXIP, else INIP) on its connection's IP."""
        s = ACTIVE_SESSIONS.get(persona_id)
        if s is None:
            return None
        port = 3659
        body = network_pair_body(s.network_addr)
        if body:
            for group in ("EXIP", "INIP"):
                f = find_tdf_field(body, group, 0x03)
                if f is not None and find_tdf_int(f[4:], "PORT", 0):
                    port = find_tdf_int(f[4:], "PORT", 0)
                    break
        return (s.client_peer[0], port)

    def received(self, port, data, addr):
        key = self.owner.get(port)
        if key is None:
            return
        to_id, about_id = key
        self.endpoints[to_id] = addr
        out_port = self.port_for(about_id, to_id)
        target = self.endpoints.get(about_id) or self.fallback_endpoint(about_id)
        transport = self.transports.get(out_port)
        if target is None or transport is None:
            return
        transport.sendto(data, target)
        self.packets += 1
        if self.packets in (1, 10, 100, 1000) or self.packets % 10000 == 0:
            log.info("[RELAY] %d packets relayed (last %d -> %d, %d bytes)", self.packets, to_id, about_id, len(data))


RELAY = RelayHub()


class _RelayPort(asyncio.DatagramProtocol):
    def __init__(self, port):
        self.port = port

    def connection_made(self, transport):
        RELAY.transports[self.port] = transport

    def datagram_received(self, data, addr):
        RELAY.received(self.port, data, addr)


def network_pair_body(raw_address):
    """EXIP/INIP fields inside a NetworkAddress union (tag, type, active 2, VALU group, body, end)."""
    if raw_address and len(raw_address) >= 10 and raw_address[3] == 0x06 and raw_address[4] == 2:
        return raw_address[9:-1]
    return None


def relay_pair_body(about_id):
    """EXIP and INIP both = the relay port for (recipient, about) on the server address the recipient uses."""
    to_id = _RELAY_TO.get()
    if not RELAY_ENABLED or to_id is None or about_id is None or to_id == about_id:
        return None
    to_session = ACTIVE_SESSIONS.get(to_id)
    port = RELAY.port_for(to_id, about_id)
    if to_session is None or port is None:
        return None
    ip = host_ipv4(host_for_peer(to_session.client_peer[0]))
    one = tdf_int_field("IP", ip) + tdf_int_field("PORT", port)
    return tdf_group_field("EXIP", one) + tdf_group_field("INIP", one)


def make_network_address_field(tag: str, raw_address=None, subject_id=None) -> bytes:
    relay = relay_pair_body(subject_id)
    if relay is not None:
        return encode_tag(tag, 0x06) + b"\x02" + tdf_group_field("VALU", relay)
    if raw_address and len(raw_address) >= 5 and raw_address[3] == 0x06:
        return encode_tag(tag, 0x06) + raw_address[4:]
    return _fallback_network_address(tag)


def make_host_network_list(raw_address=None, subject_id=None) -> bytes:
    """Encode NFS13's list-of-group representation of NetworkAddress."""
    address = make_network_address_field("PNET", raw_address, subject_id)
    # PNET union: tag, active member 2, VALU group tag, pair body, terminator.
    if (len(address) >= 10 and address[4] == 2 and
            address[5:9] == encode_tag("VALU", 0x03) and
            address[-1:] == b"\x00"):
        pair_body = address[9:-1]
    else:
        pair_body = _fallback_network_address("PNET")[9:-1]
    return (encode_tag("HNET", 0x04) + b"\x03" + tdf_varint(1) +
            b"\x02" + pair_body + b"\x00")


def make_network_qos_field(raw_qdat=None) -> bytes:
    if raw_qdat and len(raw_qdat) >= 5 and raw_qdat[3] == 0x03:
        return encode_tag("NQOS", 0x03) + raw_qdat[4:]
    body = bytearray()
    body += tdf_int_field("DBPS", 0)
    body += tdf_int_field("NATT", 1)  # Moderate LAN/NAT fallback
    body += tdf_int_field("UBPS", 0)
    return tdf_group_field("NQOS", body)


def game_protocol_hash(version: str) -> int:
    value = 2166136261
    for byte in version.encode("latin1"):
        value = ((value * 16777619) ^ byte) & 0xFFFFFFFFFFFFFFFF
    return value


def make_replicated_game_player(game_id, raw_address=None, player_attrs=None,
                                player_id=1000001, player_name="Player",
                                slot=0, state=2, custom_data=b"",
                                team_index=0xFFFF) -> bytes:
    payload = bytearray()
    payload += tdf_blob_field("BLOB", custom_data)
    payload += tdf_int_field("EXID", 0)
    payload += tdf_int_field("GID", game_id)
    payload += tdf_int_field("LOC", 0)
    payload += tdf_string_field("NAME", player_name)
    payload += (player_attrs if player_attrs is not None
                else tdf_empty_map("PATT", 0x01, 0x01))
    payload += tdf_int_field("PID", player_id)
    payload += make_network_address_field("PNET", raw_address, player_id)
    payload += tdf_int_field("SID", slot)
    payload += tdf_int_field("SLOT", 0)
    payload += tdf_int_field("STAT", state)
    payload += tdf_int_field("TIDX", team_index)
    payload += tdf_int_field("TIME", 0)
    payload += tdf_object_id_field("UGID")
    payload += tdf_int_field("UID", player_id)
    payload.append(0x00)
    return bytes(payload)


def effective_topology(requested: int) -> int:
    """Host player games cannot use the client's requested topology 0x84.

    Under 0x84 (and 1) 0x00C0A380 leaves the host out of the ConnApi client
    list as if it were a dedicated server.  The host (slot 0) and the first
    joiner (slot 1) then share index 0, and 0x00B84430 refuses the host with
    -3.  Full mesh 0x82 keeps every player at its own slot.
    """
    return 0x82 if requested == 0x84 else requested


def make_game_setup_notification_payload(request_payload=b"", game_id=1,
                                         matchmaking_session_id=1,
                                         raw_address=None, raw_qdat=None,
                                         setup_kind="matchmaking",
                                         player_id=1000001,
                                         player_name="Player", roster=None,
                                         recipient_id=None,
                                         matchmaking_result=2,
                                         game_record=None) -> bytes:
    relay_token = _RELAY_TO.set(recipient_id)  # addresses in this payload are for this player (relay ports)
    try:
        return _make_game_setup_notification_payload(
            request_payload, game_id, matchmaking_session_id, raw_address, raw_qdat, setup_kind, player_id,
            player_name, roster, recipient_id, matchmaking_result, game_record)
    finally:
        _RELAY_TO.reset(relay_token)


def _make_game_setup_notification_payload(request_payload, game_id, matchmaking_session_id, raw_address, raw_qdat,
                                          setup_kind, player_id, player_name, roster, recipient_id,
                                          matchmaking_result, game_record) -> bytes:
    host_slot = 0
    if roster:
        host_member = roster[0]
        if game_record is not None:
            host_member = next(
                (member for member in roster
                 if member.session.profile.persona_id == game_record.host_id),
                host_member,
            )
        host_slot = host_member.slot
        host_profile = host_member.session.profile
        player_id = host_profile.persona_id
        player_name = host_profile.name
        raw_address = host_member.session.network_addr or raw_address
        raw_qdat = host_member.session.qdat or raw_qdat
    if recipient_id is None:
        recipient_id = player_id
    attrs = find_tdf_field(request_payload, "ATTR", 0x05)
    player_attrs = find_tdf_field(request_payload, "PATT", 0x05)
    request_address = (find_tdf_field(request_payload, "PNET", 0x06) or
                       find_tdf_field(request_payload, "HNET", 0x06) or
                       raw_address)
    version = (find_tdf_string(request_payload, "GVER") or
               find_tdf_string(request_payload, "VSTR") or
               GAME_SERVICE_NAME)
    game_name = find_tdf_string(request_payload, "GNAM", player_name)
    capacity = max(1, find_tdf_int(request_payload, "PMAX", 8))
    settings = find_tdf_int(request_payload, "GSET", 0x11F)
    topology = effective_topology(find_tdf_int(request_payload, "NTOP", 0))
    voip = find_tdf_int(request_payload, "VOIP", 2)
    game_state = 1
    presence = find_tdf_int(request_payload, "PRES", 1)
    team_capacity = find_tdf_int(request_payload, "TCAP", 0)
    admins = (player_id,)
    team_ids = ()
    xnet_nonce = find_tdf_blob(request_payload, "XNNC")
    xnet_session = find_tdf_blob(request_payload, "XSES")
    if game_record is not None:
        attrs = game_record.attributes or attrs
        capacity = game_record.capacity
        settings = game_record.settings
        game_state = game_record.state
        presence = game_record.presence
        team_capacity = game_record.team_capacity
        admins = tuple(sorted(game_record.admins or {game_record.host_id}))
        team_ids = tuple(game_record.team_ids)
        xnet_nonce = game_record.xnet_nonce
        xnet_session = game_record.xnet_session

    game = bytearray()
    game += tdf_int_list("ADMN", admins)
    game += attrs if attrs is not None else tdf_empty_map("ATTR", 0x01, 0x01)
    game += tdf_int_list("CAP", (capacity, 0))
    game += tdf_empty_map("CRIT", 0x01, 0x01)
    game += tdf_int_field("GID", game_id)
    game += tdf_string_field("GNAM", game_name)
    game += tdf_int_field("GPVH", game_protocol_hash(version))
    game += tdf_int_field("GSET", settings)
    game += tdf_int_field("GSID", 18014398695176361)
    game += tdf_int_field("GSTA", game_state)
    game += tdf_string_field("GTYP", "")
    game += make_host_network_list(request_address, player_id)
    game += tdf_int_field("HSES", player_id)
    game += tdf_int_field("IGNO", 0)
    game += tdf_empty_map("MATR", 0x01, 0x01)
    game += tdf_int_field("MCAP", capacity)
    game += make_network_qos_field(raw_qdat)
    game += tdf_int_field("NRES", 0)
    game += tdf_int_field("NTOP", topology)
    game += tdf_string_field("PGID", "")
    game += tdf_blob_field("PGSR")
    game += tdf_group_field(
        "PHST", tdf_int_field("HPID", player_id) +
        tdf_int_field("HSLT", host_slot)
    )
    game += tdf_int_field("PRES", presence)
    game += tdf_string_field("PSAS", "local")
    game += tdf_int_field("QCAP", 0)
    game += tdf_int_field("SEED", 0x4E465331)
    game += tdf_int_field("TCAP", team_capacity)
    game += tdf_group_field(
        "THST", tdf_int_field("HPID", player_id) +
        tdf_int_field("HSLT", host_slot)
    )
    game += tdf_int_list("TIDS", team_ids)
    game += tdf_string_field("UUID", "286a2373-3e6e-46b9-8294-3ef05e479503")
    game += tdf_int_field("VOIP", voip)
    game += tdf_string_field("VSTR", version)
    game += tdf_blob_field("XNNC", xnet_nonce)
    game += tdf_blob_field("XSES", xnet_session)

    if roster:
        roster_payloads = [
            make_replicated_game_player(
                game_id,
                member.session.network_addr,
                member.player_attrs,
                member.session.profile.persona_id,
                member.session.profile.name,
                slot=member.slot,
                state=member.state,
                custom_data=member.custom_data,
                team_index=member.team_index,
            )
            for member in roster
        ]
    else:
        roster_payloads = [make_replicated_game_player(
            game_id, request_address, player_attrs, player_id, player_name,
            state=(2 if setup_kind == "matchmaking" else 4),
        )]

    payload = bytearray()
    payload += tdf_group_field("GAME", game)
    payload += encode_tag("PROS", 0x04) + b"\x03" + tdf_varint(
        len(roster_payloads)
    ) + b"".join(roster_payloads)
    payload += tdf_empty_list("QUEU", 0x03)
    payload += encode_tag("REAS", 0x06)
    if setup_kind == "matchmaking":
        reason = bytearray()
        reason += tdf_int_field("FIT", 100)
        reason += tdf_int_field("MAXF", 100)
        reason += tdf_int_field("MSID", matchmaking_session_id)
        reason += tdf_int_field("RSLT", matchmaking_result)
        reason += tdf_int_field("USID", recipient_id)
        payload += b"\x03" + tdf_group_field("VALU", reason)
    else:
        context = 0 if setup_kind == "create" else 1
        payload += b"\x00" + tdf_group_field(
            "VALU", tdf_int_field("DCTX", context)
        )
    payload.append(0x00)
    return bytes(payload)


def make_matchmaking_async_status(session_id=1, player_id=1000001,
                                  capacity=8, matched_players=0) -> bytes:
    status = bytearray()
    status += tdf_group_field(
        "CGS", tdf_int_field("EVST", 6) + tdf_int_field("MMSN", 1) +
        tdf_int_field("NOMP", matched_players)
    )
    status += tdf_group_field("CUST")
    status += tdf_group_field(
        "DNFS", tdf_int_field("MDNF", 0) + tdf_int_field("XDNF", 0)
    )
    status += tdf_group_field("FGS", tdf_int_field("GNUM", 1))
    status += tdf_group_field("GEOS", tdf_int_field("DIST", 0))
    status += tdf_empty_map("GRDA", 0x01, 0x03)
    status += tdf_group_field(
        "GSRD", tdf_int_field("PMAX", capacity) + tdf_int_field("PMIN", 1)
    )
    status += tdf_group_field("HBRD", tdf_int_field("BVAL", 1))
    status += tdf_group_field("HVRD", tdf_int_field("VVAL", 0))
    status += tdf_group_field("PSRS")
    status += tdf_group_field("RRDA", tdf_int_field("RVAL", 0))
    status += tdf_group_field(
        "TSRS", tdf_int_field("TMAX", 0) + tdf_int_field("TMIN", 0)
    )
    status += tdf_group_field("VGRS", tdf_int_field("VVAL", 0))

    payload = bytearray()
    payload += encode_tag("ASIL", 0x04) + b"\x03\x01" + bytes(status) + b"\x00"
    payload += tdf_int_field("MSID", session_id)
    payload += tdf_int_field("USID", player_id)
    payload.append(0x00)
    return bytes(payload)


def make_matchmaking_failed_payload(session_id: int, user_session_id: int,
                                    result: int = 4) -> bytes:
    """Blaze 3 NotifyMatchmakingFailed; result 4 is SESSION_CANCELED."""
    return (tdf_int_field("MAXF", 0) +
            tdf_int_field("MSID", session_id) +
            tdf_int_field("RSLT", result) +
            tdf_int_field("USID", user_session_id) + b"\x00")


def allocate_game_id() -> int:
    global NEXT_GAME_ID
    game_id = NEXT_GAME_ID
    NEXT_GAME_ID += 1
    return game_id


def allocate_matchmaking_id() -> int:
    global NEXT_MATCHMAKING_ID
    session_id = NEXT_MATCHMAKING_ID
    NEXT_MATCHMAKING_ID += 1
    return session_id


def allocate_game_list_id() -> int:
    global NEXT_GAME_LIST_ID
    list_id = NEXT_GAME_LIST_ID
    NEXT_GAME_LIST_ID += 1
    return list_id


def create_game_record(session: ClientSession, request_payload: bytes,
                       connected_host=False, matchmaking=False) -> GameRecord:
    game_id = allocate_game_id()
    capacity = max(1, find_tdf_int(request_payload, "PMAX", 8))
    game = GameRecord(
        game_id=game_id,
        host_id=session.profile.persona_id,
        request_payload=request_payload,
        capacity=capacity,
        settings=find_tdf_int(request_payload, "GSET", 0x11F),
        version=(find_tdf_string(request_payload, "GVER") or
                 find_tdf_string(request_payload, "VSTR") or
                 GAME_SERVICE_NAME),
        name=find_tdf_string(request_payload, "GNAM", session.profile.name),
        matchmaking=matchmaking,
        attributes=find_tdf_field(request_payload, "ATTR", 0x05),
        presence=find_tdf_int(request_payload, "PRES", 1),
        team_capacity=find_tdf_int(request_payload, "TCAP", 0),
        xnet_nonce=find_tdf_blob(request_payload, "XNNC"),
        xnet_session=find_tdf_blob(request_payload, "XSES"),
        admins={session.profile.persona_id},
        team_ids=list(find_tdf_int_list(request_payload, "TIDS")),
    )
    game.members[session.profile.persona_id] = GameMember(
        session=session,
        slot=0,
        state=4 if connected_host else 2,
        player_attrs=find_tdf_field(request_payload, "PATT", 0x05),
        custom_data=find_tdf_blob(request_payload, "BLOB"),
        team_index=find_tdf_int(request_payload, "TIDX", 0xFFFF),
    )
    session.game_id = game_id
    ACTIVE_GAMES[game_id] = game
    log.info("Created game gid=%d host=%s/%d capacity=%d version=%s",
             game_id, session.profile.name, session.profile.persona_id,
             capacity, game.version)
    return game


def add_session_to_game(game: GameRecord, session: ClientSession,
                        request_payload: bytes) -> GameMember:
    existing = game.members.get(session.profile.persona_id)
    if existing is not None:
        return existing
    used_slots = {member.slot for member in game.members.values()}
    slot = next(index for index in range(game.capacity)
                if index not in used_slots)
    member = GameMember(
        session=session,
        slot=slot,
        state=2,
        player_attrs=find_tdf_field(request_payload, "PATT", 0x05),
        custom_data=find_tdf_blob(request_payload, "BLOB"),
        team_index=find_tdf_int(request_payload, "TIDX", 0xFFFF),
    )
    game.members[session.profile.persona_id] = member
    session.game_id = game.game_id
    log.info("Joined game gid=%d player=%s/%d slot=%d count=%d/%d",
             game.game_id, session.profile.name, session.profile.persona_id,
             slot, len(game.members), game.capacity)
    return member


def find_matchmaking_game(session: ClientSession):
    for game in ACTIVE_GAMES.values():
        if (session.profile.persona_id not in game.members and
                session.profile.persona_id not in game.banned_players and
                len(game.members) < game.capacity and
                (game.matchmaking or (game.settings & 0x02))):
            return game
    return None


def make_player_joining_payload(game: GameRecord, member: GameMember, recipient_id=None) -> bytes:
    relay_token = _RELAY_TO.set(recipient_id)
    try:
        player = make_replicated_game_player(
            game.game_id,
            member.session.network_addr,
            member.player_attrs,
            member.session.profile.persona_id,
            member.session.profile.name,
            member.slot,
            member.state,
        )
    finally:
        _RELAY_TO.reset(relay_token)
    return (tdf_int_field("GID", game.game_id) +
            encode_tag("PDAT", 0x03) + player + b"\x00")


def make_player_join_completed_payload(game_id: int, player_id: int) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_int_field("PID", player_id) + b"\x00")


def make_player_state_change_payload(game_id: int, player_id: int,
                                     state: int) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_int_field("PID", player_id) +
            tdf_int_field("STAT", state) + b"\x00")


def make_player_removed_payload(game_id: int, player_id: int,
                                reason=6, title_context=0) -> bytes:
    return (tdf_int_field("CNTX", title_context) +
            tdf_int_field("GID", game_id) +
            tdf_int_field("PID", player_id) +
            tdf_int_field("REAS", reason) + b"\x00")


def make_game_removed_payload(game_id: int, reason=0) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_int_field("REAS", reason) + b"\x00")


def make_game_state_change_payload(game_id: int, state: int) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_int_field("GSTA", state) + b"\x00")


def make_host_migration_start_payload(game_id: int, host_id: int,
                                      slot: int, migration_type=2) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_int_field("HOST", host_id) +
            tdf_int_field("PMIG", migration_type) +
            tdf_int_field("SLOT", slot) + b"\x00")


def make_host_migration_finished_payload(game_id: int) -> bytes:
    return tdf_int_field("GID", game_id) + b"\x00"


def make_game_session_updated_payload(game: GameRecord) -> bytes:
    return (tdf_int_field("GID", game.game_id) +
            tdf_blob_field("XNNC", game.xnet_nonce) +
            tdf_blob_field("XSES", game.xnet_session) + b"\x00")


def make_player_custom_data_payload(game_id: int, player_id: int,
                                    custom_data: bytes) -> bytes:
    return (tdf_blob_field("CDAT", custom_data) +
            tdf_int_field("GID", game_id) +
            tdf_int_field("PID", player_id) + b"\x00")


def make_admin_list_change_payload(game_id: int, admin_id: int,
                                   operation: int, updater_id: int) -> bytes:
    return (tdf_int_field("ALST", admin_id) +
            tdf_int_field("GID", game_id) +
            tdf_int_field("OPER", operation) +
            tdf_int_field("UID", updater_id) + b"\x00")


def make_player_team_change_payload(game_id: int, player_id: int,
                                    team_index: int) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_int_field("PID", player_id) +
            tdf_int_field("TIDX", team_index) + b"\x00")


def make_game_name_change_payload(game_id: int, name: str) -> bytes:
    return (tdf_int_field("GID", game_id) +
            tdf_string_field("GNAM", name) + b"\x00")


def make_banned_list_response(player_ids) -> bytes:
    return tdf_int_list("BANM", tuple(sorted(player_ids))) + b"\x00"


async def send_notification(session: ClientSession, component: int,
                            notification: int, payload=b""):
    session.writer.write(make_blaze_notification(
        component, notification, payload
    ))
    await session.writer.drain()


NEXT_GAME_HISTORY_ID = [1]


def report_class_id(report_payload: bytes) -> int:
    """TDF class id of RPRT.GAME in a submit*GameReport request (0 if absent)."""
    rprt = find_tdf_field(report_payload, "RPRT", 0x03)
    if rprt is None:
        return 0
    f = find_tdf_field(rprt[4:], "GAME", 0x07)
    if f is None or len(f) < 6 or f[4] == 0:
        return 0
    value, _ = decode_tdf_varint(f, 5)
    return value & 0xffffffff


def make_result_notification(report_payload: bytes, history_id: int) -> bytes:
    """GameReporting ResultNotification {DATA variable, EROR, FNL, GHID, GRID}.
    DATA echoes the submitted report object (RPRT.GAME, e.g. Online::MultiplayerReport {ACCL DBSP LICN RSLT
    STAT}): the real server processed it into the same class (the client decodes its own report type).
    Not yet confirmed against a live event."""
    rprt = find_tdf_field(report_payload, "RPRT", 0x03)
    game_var = b"\x00"
    grid = 0
    if rprt is not None:
        body = rprt[4:]
        f = find_tdf_field(body, "GAME", 0x07)
        if f is not None:
            game_var = f[4:]
        grid = find_tdf_int(body, "GRID", 0)
    return (encode_tag("DATA", 0x07) + game_var + tdf_int_field("EROR", 0) + tdf_int_field("FNL", 1) +
            tdf_int_field("GHID", history_id) + tdf_int_field("GRID", grid) + b"\x00")


async def send_result_notification(session: ClientSession, report_payload: bytes):
    history_id = NEXT_GAME_HISTORY_ID[0]
    NEXT_GAME_HISTORY_ID[0] += 1
    payload = make_result_notification(report_payload, history_id)
    game = ACTIVE_GAMES.get(session.game_id)
    targets = [session]
    if game is not None:
        targets = [m.session for m in game.members.values() if m.session is not None] or [session]
    for target in targets:
        try:
            await send_notification(target, 0x001C, 0x0072, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("[GAMEREPORT TX] ResultNotification to %s failed: %s", target.profile.name, exc)
    log.info("[GAMEREPORT TX] ResultNotification ghid=%d to %s: %s", history_id,
             ",".join(t.profile.name for t in targets), tdf_dump(payload, 1500))


async def announce_friend_presence(session: ClientSession):
    """Publish a newly authenticated LAN profile to already-online friends."""
    if session.friends_announced:
        return
    session.friends_announced = True
    newcomer = session.profile
    update = make_association_update_notification(newcomer)
    for other in list(ACTIVE_SESSIONS.values()):
        if (other is session or not other.authenticated or
                other.profile.persona_id == newcomer.persona_id):
            continue
        # The new client will receive the complete friendList in getLists, but
        # UserManager still needs identities for profiles referenced by it.
        await send_notification(
            session, 0x7802, 0x0002,
            make_user_added_notification(
                other.profile.persona_id,
                other.profile.account_id,
                other.profile.name,
            ),
        )
        # Existing clients already built their list, so update it live.
        await send_notification(
            other, 0x7802, 0x0002,
            make_user_added_notification(
                newcomer.persona_id, newcomer.account_id, newcomer.name
            ),
        )
        await send_notification(other, 0x0019, 0x0001, update)
        log.info("[FRIENDS TX] added %s/%d to %s/%d",
                 newcomer.name, newcomer.persona_id,
                 other.profile.name, other.profile.persona_id)


async def announce_joining_member(game: GameRecord, newcomer: GameMember):
    """Make both sides' UserManager and GameManager aware of one another."""
    newcomer_profile = newcomer.session.profile
    for member in list(game.members.values()):
        if member is newcomer:
            continue
        profile = member.session.profile
        await send_notification(
            member.session, 0x7802, 0x0002,
            make_user_added_notification(
                newcomer_profile.persona_id,
                newcomer_profile.account_id,
                newcomer_profile.name,
            ),
        )
        await send_notification(
            newcomer.session, 0x7802, 0x0002,
            make_user_added_notification(
                profile.persona_id, profile.account_id, profile.name
            ),
        )
        await send_notification(
            member.session, 0x0004, 0x0015,
            make_player_joining_payload(game, newcomer, member.session.profile.persona_id),
        )


async def detach_session_from_game(session: ClientSession, reason=2,
                                   title_context=0):
    game = ACTIVE_GAMES.get(session.game_id)
    if game is None:
        session.game_id = 0
        return
    removed = game.members.pop(session.profile.persona_id, None)
    game.admins.discard(session.profile.persona_id)
    session.game_id = 0
    if removed is None:
        return

    remaining = list(game.members.values())
    if game.matchmaking and not game.setup_announced:
        if not remaining:
            ACTIVE_GAMES.pop(game.game_id, None)
            await broadcast_game_list_update(removed=(game.game_id,))
            log.info("Removed pending matchmaking game gid=%d", game.game_id)
        return

    removed_payload = make_player_removed_payload(
        game.game_id, session.profile.persona_id, reason, title_context
    )
    # Blaze sends PlayerRemoved to the removed player as well as every player
    # still in the game.  The former is essential for an explicit leave/kick:
    # without it that client keeps a stale local Game object indefinitely.
    notification_targets = [removed] + remaining
    for member in notification_targets:
        try:
            await send_notification(member.session, 0x0004, 0x0028,
                                    removed_payload)
        except (ConnectionError, asyncio.CancelledError):
            pass

    if not remaining:
        ACTIVE_GAMES.pop(game.game_id, None)
        # The final player still owns the replicated Game object.  Announce
        # its destruction after PlayerRemoved so the client can leave the
        # matchmaking screen and discard the room.
        try:
            await send_notification(
                removed.session, 0x0004, 0x0010,
                make_game_removed_payload(game.game_id, reason=0),
            )
        except (ConnectionError, asyncio.CancelledError):
            pass
        await broadcast_game_list_update(removed=(game.game_id,))
        log.info("Removed empty game gid=%d", game.game_id)
        return

    if game.host_id == session.profile.persona_id:
        if game.settings & 0x10:  # HOST_MIGRATABLE
            new_host = min(remaining, key=lambda item: item.slot)
            game.host_id = new_host.session.profile.persona_id
            game.admins.add(game.host_id)
            migration_start = make_host_migration_start_payload(
                game.game_id, game.host_id, new_host.slot
            )
            migration_finished = make_host_migration_finished_payload(
                game.game_id
            )
            for member in remaining:
                try:
                    await send_notification(
                        member.session, 0x0004, 0x0046, migration_start
                    )
                    await send_notification(
                        member.session, 0x0004, 0x003C, migration_finished
                    )
                except (ConnectionError, asyncio.CancelledError):
                    pass
            await broadcast_game_list_update(games=(game,))
            log.info("Migrated game gid=%d host=%d slot=%d after disconnect",
                     game.game_id, game.host_id, new_host.slot)
            return

        ACTIVE_GAMES.pop(game.game_id, None)
        game_removed = make_game_removed_payload(game.game_id, reason=2)
        for member in remaining:
            member.session.game_id = 0
            try:
                await send_notification(member.session, 0x0004, 0x0010,
                                        game_removed)
            except (ConnectionError, asyncio.CancelledError):
                pass
        await broadcast_game_list_update(removed=(game.game_id,))
        log.info("Destroyed orphaned game gid=%d after host disconnect",
                 game.game_id)
    else:
        # Keep open-game subscribers in sync with the changed roster/count.
        await broadcast_game_list_update(games=(game,))


def make_matchmaking_config_response() -> bytes:
    """Minimal complete Blaze 3 GetMatchmakingConfigResponse for NFS13."""
    predefined = bytearray()
    predefined += tdf_string_field("RNME", "pingSite")
    predefined += tdf_string_list("THLS", ("matchAny",))
    predefined += tdf_int_field("WGHT", 100)
    predefined.append(0x00)

    ping_site = bytearray()
    ping_site += encode_tag("PDRC", 0x03) + bytes(predefined)
    ping_site += tdf_string_list("POSV", ("local",))
    ping_site.append(0x00)

    payload = bytearray()
    payload += tdf_empty_list("GLST", 0x03)  # GenericRuleConfig[]
    payload += encode_tag("PPSR", 0x03) + bytes(ping_site)
    payload += tdf_empty_list("RLST", 0x03)  # PredefinedRuleConfig[]
    payload.append(0x00)
    return bytes(payload)


def make_start_matchmaking_response(session_id: int) -> bytes:
    return tdf_int_field("MSID", session_id) + b"\x00"


def make_create_game_response(game_id: int) -> bytes:
    return tdf_int_field("GID", game_id) + b"\x00"


def make_join_game_response(game_id: int, join_state=0) -> bytes:
    payload = bytearray()
    payload += tdf_int_field("GID", game_id)
    payload += tdf_int_field("JGS", join_state)
    payload.append(0x00)
    return bytes(payload)


def make_list_game_data(game: GameRecord) -> bytes:
    """Encode one Blaze 3 ListGameData using the same proven setup schema."""
    roster = sorted(game.members.values(), key=lambda item: item.slot)
    host = game.members.get(game.host_id)
    setup = make_game_setup_notification_payload(
        request_payload=game.request_payload,
        game_id=game.game_id,
        raw_address=host.session.network_addr if host is not None else None,
        raw_qdat=host.session.qdat if host is not None else None,
        setup_kind="create",
        player_id=game.host_id,
        player_name=(host.session.profile.name if host is not None else game.name),
        roster=roster,
        recipient_id=game.host_id,
        game_record=game,
    )
    game_field = find_tdf_field(setup, "GAME", 0x03)
    roster_field = find_tdf_field(setup, "PROS", 0x04)
    if game_field is None or roster_field is None:
        raise ValueError(f"unable to encode full game data for gid={game.game_id}")
    return game_field + roster_field + b"\x00"


def make_list_games_response(games=None) -> bytes:
    if games is None:
        games = sorted(ACTIVE_GAMES.values(), key=lambda item: item.game_id)
    encoded = [make_list_game_data(game) for game in games]
    return (encode_tag("LGAM", 0x04) + b"\x03" +
            tdf_varint(len(encoded)) + b"".join(encoded) + b"\x00")


def make_game_browser_player(member: GameMember) -> bytes:
    profile = member.session.profile
    payload = bytearray()
    payload += tdf_int_field("EXID", 0)
    payload += tdf_int_field("LOC", 0)
    payload += tdf_string_field("NAME", profile.name)
    payload += (member.player_attrs if member.player_attrs is not None
                else tdf_empty_map("PATT", 0x01, 0x01))
    payload += tdf_int_field("PID", profile.persona_id)
    payload += tdf_int_field("STAT", member.state)
    payload += tdf_int_field("TIDX", member.team_index)
    payload.append(0x00)
    return bytes(payload)


def make_game_browser_data(game: GameRecord) -> bytes:
    """Encode all GameBrowserGameData members expected by the generated client."""
    host = game.members.get(game.host_id)
    request = game.request_payload
    attrs = game.attributes or find_tdf_field(request, "ATTR", 0x05)
    topology = effective_topology(find_tdf_int(request, "NTOP", 0))
    voip = find_tdf_int(request, "VOIP", 2)
    roster = sorted(game.members.values(), key=lambda item: item.slot)
    roster_payloads = [make_game_browser_player(member) for member in roster]

    payload = bytearray()
    payload += tdf_int_list("ADMN", tuple(sorted(game.admins or {game.host_id})))
    payload += attrs if attrs is not None else tdf_empty_map("ATTR", 0x01, 0x01)
    payload += tdf_int_list("CAP", (game.capacity, 0))
    payload += tdf_empty_map("CRIT", 0x01, 0x01)
    payload += tdf_int_field("GID", game.game_id)
    payload += tdf_string_field("GNAM", game.name)
    payload += tdf_int_field("GSET", game.settings)
    payload += tdf_int_field("GSTA", game.state)
    payload += make_host_network_list(
        host.session.network_addr if host is not None else None, game.host_id
    )
    payload += tdf_int_field("HOST", game.host_id)
    payload += tdf_int_field("NTOP", topology)
    payload += tdf_int_list("PCNT", (len(roster), 0))
    payload += tdf_int_field("PRES", game.presence)
    payload += tdf_string_field("PSAS", "local")
    payload += tdf_string_field("PSID", "")
    payload += tdf_int_field("QCAP", 0)
    payload += tdf_int_field("QCNT", 0)
    payload += (encode_tag("ROST", 0x04) + b"\x03" +
                tdf_varint(len(roster_payloads)) + b"".join(roster_payloads))
    payload += tdf_int_field("SID", 0)
    payload += tdf_int_field("TCAP", game.team_capacity)
    payload += tdf_empty_list("TINF", 0x03)
    payload += tdf_int_field("VOIP", voip)
    payload += tdf_string_field("VSTR", game.version)
    payload.append(0x00)
    return bytes(payload)


def make_game_browser_data_list(games=None) -> bytes:
    if games is None:
        games = sorted(ACTIVE_GAMES.values(), key=lambda item: item.game_id)
    encoded = [make_game_browser_data(game) for game in games]
    return (encode_tag("GDAT", 0x04) + b"\x03" +
            tdf_varint(len(encoded)) + b"".join(encoded) + b"\x00")


def make_game_list_update(list_id: int, games=(), removed=(), done=True) -> bytes:
    matches = []
    for game in games:
        match = bytearray()
        match += tdf_int_field("FIT", 100)
        match += encode_tag("GAM", 0x03) + make_game_browser_data(game)
        match.append(0x00)
        matches.append(bytes(match))

    payload = bytearray()
    payload += tdf_int_field("DONE", 1 if done else 0)
    payload += tdf_int_field("GLID", list_id)
    payload += tdf_int_list("REMV", tuple(removed))
    payload += (encode_tag("UPDT", 0x04) + b"\x03" +
                tdf_varint(len(matches)) + b"".join(matches))
    payload.append(0x00)
    return bytes(payload)


def make_game_list_response(list_id=1, number_of_games=0) -> bytes:
    payload = bytearray()
    payload += tdf_int_field("GLID", list_id)
    payload += tdf_int_field("MAXF", 100)
    payload += tdf_int_field("NGD", number_of_games)
    payload.append(0x00)
    return bytes(payload)


async def broadcast_game_list_update(games=(), removed=()):
    """Push live deltas to clients that requested a browser subscription."""
    for target in list(ACTIVE_SESSIONS.values()):
        if not target.authenticated or not target.game_list_subscription:
            continue
        try:
            await send_notification(
                target, 0x0004, 0x00C9,
                make_game_list_update(
                    target.game_list_id, games=games, removed=removed, done=False
                ),
            )
        except (ConnectionError, asyncio.CancelledError):
            pass


async def handle_game_manager_request(session, command, msg_id, payload, peer):
    """Handle NFS13 GameManager RPCs and complete the setup notification flow."""
    writer = session.writer
    profile = session.profile
    command_name = GAME_MANAGER_COMMANDS.get(command, "unknown")
    log.info("[GAME MANAGER] %s cmd=0x%04X msg=%d payload=%d hex=%s from %s",
             command_name, command, msg_id, len(payload), payload.hex(" "), peer)

    setup_kind = None
    setup_recipients = []
    matchmaking_result = 2
    matchmaking_session_id = 0
    game = None
    newcomer = None
    announce_newcomer = False
    mesh_completed_players = []
    state_changed = False
    cancel_matchmaking = False
    browser_initial = False
    browser_changed_game = None
    browser_removed_id = 0
    destroyed_game = None
    member_notifications = []
    removals = []
    response_error = 0
    if command == 0x0068:  # getMatchmakingConfig
        payload_out = make_matchmaking_config_response()
    elif command == 0x000D:  # startMatchmaking
        matchmaking_session_id = allocate_matchmaking_id()
        session.matchmaking_id = matchmaking_session_id
        game = find_matchmaking_game(session)
        if game is None:
            game = create_game_record(session, payload, matchmaking=True)
            newcomer = game.members[profile.persona_id]
            matchmaking_result = 0  # CreatedGame
        else:
            newcomer = add_session_to_game(game, session, payload)
            announce_newcomer = game.setup_announced
        payload_out = make_start_matchmaking_response(matchmaking_session_id)
        if len(game.members) >= 2:
            setup_kind = "matchmaking"
            setup_recipients = ([newcomer] if game.setup_announced
                                else list(game.members.values()))
        browser_changed_game = game
    elif command == 0x000E:  # cancelMatchmaking
        requested_id = find_tdf_int(payload, "MSID", session.matchmaking_id)
        cancel_matchmaking = (
            session.matchmaking_id == 0 or
            requested_id in (0, session.matchmaking_id)
        )
        payload_out = b""
    elif command == 0x0001:  # createGame
        game = create_game_record(session, payload, connected_host=True)
        newcomer = game.members[profile.persona_id]
        payload_out = make_create_game_response(game.game_id)
        setup_kind = "create"
        browser_changed_game = game
    elif command in (0x0009, 0x0015):  # joinGame / joinGameByGroup
        game_id = find_tdf_int(payload, "GID", 0)
        game = ACTIVE_GAMES.get(game_id)
        if (game is not None and
                profile.persona_id not in game.banned_players and
                len(game.members) < game.capacity):
            newcomer = add_session_to_game(game, session, payload)
            announce_newcomer = len(game.members) > 1
            payload_out = (make_join_game_response(game_id)
                           if command == 0x0009 else b"")
            setup_kind = "join"
            browser_changed_game = game
        else:
            log.warning("Join requested for missing/full game gid=%d", game_id)
            payload_out = b""
            if game is None:
                response_error = 2  # GAMEMANAGER_ERR_INVALID_GAME_ID
            elif profile.persona_id in game.banned_players:
                response_error = 110  # GAMEMANAGER_ERR_PLAYER_BANNED
            else:
                response_error = 4  # GAMEMANAGER_ERR_GAME_FULL
    elif command == 0x0002:  # destroyGame
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.pop(game_id, None)
        if game is not None:
            destroyed_game = game
            for member in game.members.values():
                member.session.game_id = 0
            browser_removed_id = game_id
        payload_out = make_create_game_response(game_id)
    elif command == 0x0003:  # advanceGameState
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.state = find_tdf_int(payload, "GSTA", game.state)
            state_changed = True
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0004:  # setGameSettings
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.settings = find_tdf_int(payload, "GSET", game.settings)
            game.request_payload = replace_tdf_field(
                game.request_payload, tdf_int_field("GSET", game.settings)
            )
            member_notifications.append((
                game, 0x006E,
                tdf_int_field("ATTR", game.settings) +
                tdf_int_field("GID", game.game_id) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0005:  # setPlayerCapacity
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            capacities = find_tdf_int_list(payload, "PCAP")
            if capacities:
                requested_capacity = sum(capacities)
            else:
                requested_capacity = find_tdf_int(
                    payload, "PMAX", game.capacity
                )
                capacities = (requested_capacity, 0)
            game.capacity = max(1, len(game.members), requested_capacity)
            game.team_capacity = find_tdf_int(
                payload, "TCAP", game.team_capacity
            )
            game.request_payload = replace_tdf_field(
                game.request_payload, tdf_int_field("PMAX", game.capacity)
            )
            member_notifications.append((
                game, 0x006F,
                tdf_int_list("CAP", capacities) +
                tdf_int_field("GID", game.game_id) +
                tdf_int_field("TCAP", game.team_capacity) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0006:  # setPresenceMode
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.presence = find_tdf_int(payload, "PRES", game.presence)
            game.request_payload = replace_tdf_field(
                game.request_payload, tdf_int_field("PRES", game.presence)
            )
            member_notifications.append((
                game, 0x0078,
                tdf_int_field("GID", game.game_id) +
                tdf_int_field("PRES", game.presence) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0007:  # setGameAttributes
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        attrs = find_tdf_field(payload, "ATTR", 0x05)
        if game is not None and attrs is not None:
            game.attributes = attrs
            game.request_payload = replace_tdf_field(game.request_payload, attrs)
            member_notifications.append((
                game, 0x0050,
                attrs + tdf_int_field("GID", game.game_id) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0008:  # setPlayerAttributes
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        player_id = find_tdf_int(payload, "PID", profile.persona_id)
        attrs = find_tdf_field(payload, "ATTR", 0x05)
        member = game.members.get(player_id) if game is not None else None
        if member is not None and attrs is not None:
            # ReplicatedGamePlayer calls this same map PATT.
            member.player_attrs = encode_tag("PATT", 0x05) + attrs[4:]
            member_notifications.append((
                game, 0x005A,
                attrs + tdf_int_field("GID", game.game_id) +
                tdf_int_field("PID", player_id) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    elif command in (0x000B, 0x0016):  # removePlayer / leaveGameByGroup
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        player_id = find_tdf_int(payload, "PID", profile.persona_id)
        member = game.members.get(player_id) if game is not None else None
        if member is not None:
            removals.append((
                member.session,
                find_tdf_int(
                    payload, "REAS", 7 if command == 0x0016 else 6
                ),
                find_tdf_int(payload, "CNTX", 0),
            ))
        payload_out = b""
    elif command in (0x000F, 0x001A):  # finalize/update game session
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.xnet_nonce = find_tdf_blob(
                payload, "XNNC", game.xnet_nonce
            )
            game.xnet_session = find_tdf_blob(
                payload, "XSES", game.xnet_session
            )
            game.request_payload = replace_tdf_field(
                game.request_payload,
                tdf_blob_field("XNNC", game.xnet_nonce),
            )
            game.request_payload = replace_tdf_field(
                game.request_payload,
                tdf_blob_field("XSES", game.xnet_session),
            )
            member_notifications.append((
                game, 0x0073, make_game_session_updated_payload(game)
            ))
            if command == 0x000F:
                host = game.members.get(game.host_id)
                host_slot = host.slot if host is not None else 0
                member_notifications.append((
                    game, 0x0047,
                    tdf_int_field("GID", game.game_id) +
                    tdf_int_field("PHST", host_slot) + b"\x00",
                ))
        payload_out = b""
    elif command == 0x0012:  # setPlayerCustomData
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        player_id = find_tdf_int(payload, "PID", profile.persona_id)
        member = game.members.get(player_id) if game is not None else None
        if member is not None:
            member.custom_data = find_tdf_blob(payload, "CDAT")
            member_notifications.append((
                game, 0x005F,
                make_player_custom_data_payload(
                    game.game_id, player_id, member.custom_data
                ),
            ))
        payload_out = b""
    elif command == 0x0013:  # replayGame
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.state = 130  # PRE_GAME
            state_changed = True
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0017:  # migrateGame
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        new_host_id = find_tdf_int(payload, "HOST", 0)
        new_host = game.members.get(new_host_id) if game is not None else None
        if game is not None and new_host is not None:
            old_host_id = game.host_id
            game.host_id = new_host_id
            game.admins.discard(old_host_id)
            game.admins.add(new_host_id)
            member_notifications.append((
                game, 0x0046,
                make_host_migration_start_payload(
                    game.game_id, new_host_id, new_host.slot
                ),
            ))
            member_notifications.append((
                game, 0x003C,
                make_host_migration_finished_payload(game.game_id),
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0018:  # updateGameHostMigrationStatus
        # Migration completion is broadcast eagerly above; clients still send
        # this acknowledgement and expect a successful empty response.
        payload_out = b""
    elif command == 0x001B:  # banPlayer
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        title_context = find_tdf_int(payload, "CNTX", 0)
        if game is not None:
            for player_id in find_tdf_int_list(payload, "PLST"):
                game.banned_players.add(player_id)
                member = game.members.get(player_id)
                if member is not None:
                    removals.append((member.session, 9, title_context))
        payload_out = b""
    elif command == 0x001D:  # updateMeshConnection
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            targets = find_tdf_group_list(payload, "TARG")
            # A few client paths omit TARG and report their own connection.
            if not targets:
                targets = (tdf_int_field("PID", profile.persona_id) +
                           tdf_int_field("STAT", 2) + b"\x00",)
            for target in targets:
                player_id = find_tdf_int(
                    target, "PID", profile.persona_id
                )
                connection_state = find_tdf_int(target, "STAT", 0)
                member = game.members.get(player_id)
                if (member is not None and connection_state == 2 and
                        member.state != 4):
                    member.state = 4
                    member.session.matchmaking_id = 0
                    mesh_completed_players.append((game, player_id))
            if mesh_completed_players:
                browser_changed_game = game
        payload_out = b""
    elif command == 0x001F:  # removePlayerFromBannedList
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.banned_players.discard(find_tdf_int(payload, "PID", 0))
        payload_out = b""
    elif command == 0x0020:  # clearBannedList
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.banned_players.clear()
        payload_out = b""
    elif command == 0x0021:  # getBannedList
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        payload_out = make_banned_list_response(
            sorted(game.banned_players) if game is not None else ()
        )
    elif command == 0x0027:  # updateGameName
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        if game is not None:
            game.name = find_tdf_string(payload, "GNAM", game.name)
            game.request_payload = replace_tdf_field(
                game.request_payload, tdf_string_field("GNAM", game.name)
            )
            member_notifications.append((
                game, 0x00E6,
                tdf_int_field("GID", game.game_id) +
                tdf_string_field("GNAM", game.name) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x0011:  # listGames
        payload_out = make_list_games_response()
    elif command in (0x0064, 0x0065):
        session.game_list_id = allocate_game_list_id()
        session.game_list_subscription = command == 0x0065
        payload_out = make_game_list_response(
            session.game_list_id, len(ACTIVE_GAMES)
        )
        browser_initial = True
    elif command == 0x0066:  # destroyGameList
        requested_list = find_tdf_int(payload, "GLID", session.game_list_id)
        if requested_list == session.game_list_id:
            session.game_list_id = 0
            session.game_list_subscription = False
        payload_out = b""
    elif command == 0x0067:  # getFullGameData
        requested_ids = find_tdf_int_list(payload, "GIDL")
        selected = tuple(
            ACTIVE_GAMES[game_id] for game_id in requested_ids
            if game_id in ACTIVE_GAMES
        )
        payload_out = make_list_games_response(selected)
    elif command == 0x0069:  # getGameDataFromId
        requested_ids = find_tdf_int_list(payload, "GLST")
        selected = tuple(
            ACTIVE_GAMES[game_id] for game_id in requested_ids
            if game_id in ACTIVE_GAMES
        )
        if selected:
            payload_out = make_game_browser_data_list(selected)
        else:
            payload_out = b""
            response_error = 2  # GAMEMANAGER_ERR_INVALID_GAME_ID
    elif command in (0x006A, 0x006B, 0x006E):
        # addAdminPlayer / removeAdminPlayer / migrateAdminPlayer
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        admin_id = find_tdf_int(payload, "PID", profile.persona_id)
        if game is not None:
            if command == 0x006A:
                game.admins.add(admin_id)
                operation = 0
            elif command == 0x006B:
                game.admins.discard(admin_id)
                operation = 1
            else:
                game.admins.discard(profile.persona_id)
                game.admins.add(admin_id)
                operation = 2
            member_notifications.append((
                game, 0x00CA,
                make_admin_list_change_payload(
                    game.game_id, admin_id, operation, profile.persona_id
                ),
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x006C:  # setPlayerTeam
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        player_id = find_tdf_int(payload, "PID", profile.persona_id)
        member = game.members.get(player_id) if game is not None else None
        if member is not None:
            member.team_index = find_tdf_int(
                payload, "TIDX", member.team_index
            )
            member_notifications.append((
                game, 0x0075,
                make_player_team_change_payload(
                    game.game_id, player_id, member.team_index
                ),
            ))
            browser_changed_game = game
        payload_out = b""
    elif command == 0x006D:  # changeGameTeamId
        game_id = find_tdf_int(payload, "GID", session.game_id)
        game = ACTIVE_GAMES.get(game_id)
        team_index = find_tdf_int(payload, "TIDX", 0)
        new_team_id = find_tdf_int(payload, "NTID", 0xFFFF)
        if game is not None:
            while len(game.team_ids) <= team_index:
                game.team_ids.append(0xFFFF)
            old_team_id = game.team_ids[team_index]
            game.team_ids[team_index] = new_team_id
            member_notifications.append((
                game, 0x0076,
                tdf_int_field("GID", game.game_id) +
                tdf_int_field("NTID", new_team_id) +
                tdf_int_field("OTID", old_team_id) +
                tdf_int_field("TIDX", team_index) + b"\x00",
            ))
            browser_changed_game = game
        payload_out = b""
    else:
        payload_out = b""

    response = make_blaze_response(
        0x0004, command, msg_id, payload_out, error=response_error
    )
    log.info("[GAME MANAGER TX] %s msg=%d payload=%d hex=%s",
             command_name, msg_id, len(payload_out), payload_out.hex(" "))
    writer.write(response)
    await writer.drain()

    if destroyed_game is not None:
        # Complete the RPC callback before invalidating the replicated game on
        # clients.  All members (including the host/requester) own that object.
        removed_payload = make_game_removed_payload(
            destroyed_game.game_id, reason=0  # SYS_GAME_ENDING
        )
        for member in list(destroyed_game.members.values()):
            try:
                await send_notification(
                    member.session, 0x0004, 0x0010, removed_payload
                )
            except (ConnectionError, asyncio.CancelledError):
                pass
        log.info("[GAME MANAGER TX] GameRemoved gid=%d members=%d",
                 destroyed_game.game_id, len(destroyed_game.members))

    for changed_game, notification, notification_payload in member_notifications:
        for member in list(changed_game.members.values()):
            await send_notification(
                member.session, 0x0004, notification, notification_payload
            )

    if browser_initial:
        # GetGameListResponse only announces the count; rows are delivered by
        # the protocol's required asynchronous GameListUpdate notification.
        await asyncio.sleep(0.050)
        initial_payload = make_game_list_update(
            session.game_list_id,
            games=sorted(ACTIVE_GAMES.values(), key=lambda item: item.game_id),
            done=True,
        )
        await send_notification(session, 0x0004, 0x00C9, initial_payload)
        log.info("[GAME MANAGER TX] GameListUpdate list=%d games=%d",
                 session.game_list_id, len(ACTIVE_GAMES))

    if browser_changed_game is not None:
        await broadcast_game_list_update(games=(browser_changed_game,))
    elif browser_removed_id:
        await broadcast_game_list_update(removed=(browser_removed_id,))

    if announce_newcomer and game is not None and newcomer is not None:
        await announce_joining_member(game, newcomer)

    for mesh_game, player_id in mesh_completed_players:
        state_payload = make_player_state_change_payload(
            mesh_game.game_id, player_id, 4
        )
        completed_payload = make_player_join_completed_payload(
            mesh_game.game_id, player_id
        )
        for member in list(mesh_game.members.values()):
            await send_notification(member.session, 0x0004, 0x0074,
                                    state_payload)
            await send_notification(member.session, 0x0004, 0x001E,
                                    completed_payload)
        log.info("[GAME MANAGER TX] mesh complete gid=%d player=%d",
                 mesh_game.game_id, player_id)

    if state_changed and game is not None:
        state_payload = make_game_state_change_payload(game.game_id, game.state)
        for member in list(game.members.values()):
            await send_notification(member.session, 0x0004, 0x0064,
                                    state_payload)
        log.info("[GAME MANAGER TX] game state gid=%d state=0x%X",
                 game.game_id, game.state)

    for removed_session, removal_reason, title_context in removals:
        await detach_session_from_game(
            removed_session, reason=removal_reason,
            title_context=title_context,
        )

    if cancel_matchmaking:
        canceled_matchmaking_id = session.matchmaking_id
        session.matchmaking_id = 0
        if session.game_id:
            await detach_session_from_game(session, reason=6)
        if canceled_matchmaking_id:
            failed_payload = make_matchmaking_failed_payload(
                canceled_matchmaking_id, profile.persona_id
            )
            await send_notification(session, 0x0004, 0x000A, failed_payload)
            log.info("[GAME MANAGER TX] NotifyMatchmakingFailed canceled msid=%d player=%d",
                     canceled_matchmaking_id, profile.persona_id)
        log.info("[GAME MANAGER TX] matchmaking canceled player=%d request=%d",
                 profile.persona_id, requested_id)

    if command == 0x000D and setup_kind is None:
        await asyncio.sleep(0.050)
        async_payload = make_matchmaking_async_status(
            matchmaking_session_id, profile.persona_id,
            capacity=game.capacity, matched_players=0,
        )
        writer.write(make_blaze_notification(0x0004, 0x000C, async_payload))
        await writer.drain()
        log.info("[GAME MANAGER TX] waiting for second player gid=%d player=%d",
                 game.game_id, profile.persona_id)

    if setup_kind is not None:
        # The generated client must complete the RPC callback before it sees
        # async matchmaking/game setup, matching the working auth ordering.
        await asyncio.sleep(0.050)
        if setup_kind == "matchmaking":
            async_payload = make_matchmaking_async_status(
                matchmaking_session_id, profile.persona_id,
                capacity=game.capacity,
                matched_players=len(game.members) - 1,
            )
            async_frame = make_blaze_notification(
                0x0004, 0x000C, async_payload
            )
            writer.write(async_frame)
            await writer.drain()
            log.info("[GAME MANAGER TX] MatchmakingAsyncStatus payload=%d hex=%s",
                     len(async_payload), async_payload.hex(" "))
            if len(setup_recipients) > 1:
                host_session = game.members[game.host_id].session
                host_status = make_matchmaking_async_status(
                    host_session.matchmaking_id,
                    host_session.profile.persona_id,
                    capacity=game.capacity,
                    matched_players=len(game.members) - 1,
                )
                host_session.writer.write(
                    make_blaze_notification(0x0004, 0x000C, host_status)
                )
                await host_session.writer.drain()
            await asyncio.sleep(0.050)

        setup_payload = make_game_setup_notification_payload(
            request_payload=game.request_payload,
            game_id=game.game_id,
            matchmaking_session_id=matchmaking_session_id,
            raw_address=game.members[game.host_id].session.network_addr,
            raw_qdat=game.members[game.host_id].session.qdat,
            setup_kind=setup_kind,
            player_id=profile.persona_id,
            player_name=profile.name,
            roster=sorted(game.members.values(), key=lambda item: item.slot),
            recipient_id=profile.persona_id,
            matchmaking_result=matchmaking_result,
            game_record=game,
        )
        setup_frame = make_blaze_notification(0x0004, 0x0014, setup_payload)
        writer.write(setup_frame)
        await writer.drain()
        log.info("[GAME MANAGER TX] NotifyGameSetup kind=%s payload=%d hex=%s",
                 setup_kind, len(setup_payload), setup_payload.hex(" "))
        if setup_kind == "matchmaking" and len(setup_recipients) > 1:
            host_session = game.members[game.host_id].session
            host_payload = make_game_setup_notification_payload(
                request_payload=game.request_payload,
                game_id=game.game_id,
                matchmaking_session_id=host_session.matchmaking_id,
                raw_address=host_session.network_addr,
                raw_qdat=host_session.qdat,
                setup_kind="matchmaking",
                player_id=host_session.profile.persona_id,
                player_name=host_session.profile.name,
                roster=sorted(game.members.values(), key=lambda item: item.slot),
                recipient_id=host_session.profile.persona_id,
                matchmaking_result=0,
                game_record=game,
            )
            host_session.writer.write(
                make_blaze_notification(0x0004, 0x0014, host_payload)
            )
            await host_session.writer.drain()
            log.info("[GAME MANAGER TX] NotifyGameSetup kind=matchmaking host=%d payload=%d",
                     host_session.profile.persona_id, len(host_payload))
        if (setup_kind in ("matchmaking", "join") and
                profile.persona_id != game.host_id):
            # 0x00BB64D0 defers the joiner's connections while 0x00BBDF90 is
            # still creating the mesh, then drops them.  A promoted-player
            # notification reaches it again through 0x00BC54A0, once per peer.
            for member in sorted(game.members.values(), key=lambda item: item.slot):
                if member.session is session:
                    continue
                writer.write(make_blaze_notification(
                    0x0004, 0x0018, make_player_joining_payload(game, member, profile.persona_id)
                ))
                await writer.drain()
                log.info("[GAME MANAGER TX] NotifyPlayerPromotedFromQueue gid=%d "
                         "to=%d peer=%d", game.game_id, profile.persona_id,
                         member.session.profile.persona_id)
        game.setup_announced = True

async def handle_auth_request(session, command, msg_id, payload, peer):
    writer = session.writer
    profile = session.profile
    cmd_name = AUTH_COMMANDS.get(command, "unknown")
    log.info("[AUTH] %s cmd=0x%04X msg=%d payload=%d tdf=%s from %s",
             cmd_name, command, msg_id, len(payload), tdf_dump(payload), peer)

    # Wii U: wiiULogin carries the Nintendo independent service token (TCKT). The command id is not in the
    # PC table; recognise it by the ticket (or the remembered id) and accept any ticket.
    if command in (WIIU_LOGIN_COMMAND[0], 0x00C9, 0x00CA) or (
            command not in AUTH_COMMANDS and find_tdf_field(payload, "TOKN", 0x01) is not None):
        if WIIU_LOGIN_COMMAND[0] != command:
            log.info("[AUTH] wiiULogin is command 0x%04X", command)
            WIIU_LOGIN_COMMAND[0] = command
        # The patched client puts the account's Nintendo Network ID in TOKN; the player is named after it.
        nnid = find_tdf_string(payload, "TOKN", "")
        if TOKEN_RE.fullmatch(nnid):
            profile = PLAYER_REGISTRY.get_for_token(nnid, session.client_peer[0])
            session.profile = profile
        elif NNID_RE.fullmatch(nnid) and nnid != "nfsmwu-local":
            profile = PLAYER_REGISTRY.get_for_nnid(nnid, session.client_peer[0])
            session.profile = profile
        else:
            # No NNID (Cemu never returns one). Several clients from one address (two Cemus on a PC) must still
            # be different players: take the next per-address profile that is not logged in right now.
            ip = session.client_peer[0]
            k = 1
            while True:
                active = ACTIVE_SESSIONS.get(profile.persona_id)
                if active is None or active is session or active.writer.is_closing():
                    break
                k += 1
                profile = PLAYER_REGISTRY.get(f"{ip}#{k}")
            session.profile = profile
            log.info("[AUTH] no NNID in ticket (%r): using %s", nnid, profile.name)
        session_key = secrets.token_hex(16)
        payload_out = make_wiiu_login_response(profile.name, profile.persona_id,
                                               profile.account_id, session_key)
        writer.write(make_blaze_response(0x0001, command, msg_id, payload_out))
        await writer.drain()
        log.info("[LOGIN TX] wiiULogin msg=%d %s", msg_id, tdf_dump(payload_out))
        await asyncio.sleep(0.050)
        writer.write(make_blaze_notification(
            0x7802, 0x0008,
            make_user_authenticated_notification(
                profile.persona_id, profile.account_id, profile.name, session_key)))
        await writer.drain()
        session.authenticated = True
        session.wiiu_session_key = session_key
        ACTIVE_SESSIONS[profile.persona_id] = session
        return

    if command == 0x0024:  # getAuthToken -> AUTH; the Wii U sends it to Autolog as ?session=
        token = getattr(session, "wiiu_session_key", "") or secrets.token_hex(16)
        session.wiiu_session_key = token
        payload_out = tdf_string_field("AUTH", token) + b"\x00"
        writer.write(make_blaze_response(0x0001, command, msg_id, payload_out))
        await writer.drain()
        log.info("[AUTH TX] getAuthToken %s", tdf_dump(payload_out))
        return
    if command in (0x0020, 0x001D):  # listEntitlements / listUserEntitlements2 -> NLST
        # The game's Online Pass table (rodata 0x1001d188, 28-byte rows: tag, project id, product id) - grant
        # every row so EntitlementManager::HasOnlinePass holds. Group NFS13CAFE as requested.
        rows = [("VALIDATED_ONLINE_ACCESS", "", "DR:106479900"),
                ("ONLINE_ACCESS", "254017", ""), ("ONLINE_ACCESS", "254016", ""),
                ("ONLINE_ACCESS", "252603", "DR:127084900"), ("ONLINE_ACCESS", "300368", "OFB-NWNA:22766"),
                ("ONLINE_ACCESS", "301762", ""), ("ONLINE_ACCESS", "302418", "")]
        group = (find_tdf_string(payload, "GNLS") or "NFS13CAFE")
        items = b""
        for i, (tag, project, product) in enumerate(rows):
            items += (tdf_string_field("DEVI", "") + tdf_string_field("GDAY", "2013-03-01T00:00Z") +
                      tdf_string_field("GNAM", "NFS13CAFE") + tdf_int_field("ID", 5000 + i) +
                      tdf_int_field("ISCO", 0) + tdf_int_field("PID", profile.persona_id) +
                      tdf_string_field("PJID", project) + tdf_int_field("PRCA", 0) +
                      tdf_string_field("PRID", product) + tdf_int_field("STAT", 1) +
                      tdf_int_field("STRC", 0) + tdf_string_field("TAG", tag) +
                      tdf_string_field("TDAY", "") + tdf_int_field("TYPE", 1) +
                      tdf_int_field("UCNT", 0) + tdf_int_field("VER", 0) + b"\x00")
        payload_out = encode_tag("NLST", 0x04) + b"\x03" + tdf_varint(len(rows)) + items + b"\x00"
        del group
        writer.write(make_blaze_response(0x0001, command, msg_id, payload_out))
        await writer.drain()
        log.info("[AUTH TX] %s -> %s", cmd_name, tdf_dump(payload_out, 600))
        return
    if command in (0x00E6, 0x0104):  # createWalUserSession / user options: void responses
        writer.write(make_blaze_response(0x0001, command, msg_id, b""))
        await writer.drain()
        return

    if command == 0x0046: # logout
        log.info("[+] Responding to logout (0x0046) with empty 12-byte response")
        resp = make_blaze_response(0x0001, command, msg_id, b"")
        writer.write(resp)
        await writer.drain()
        session.authenticated = False
        if ACTIVE_SESSIONS.get(profile.persona_id) is session:
            ACTIVE_SESSIONS.pop(profile.persona_id, None)
    elif command == 0x0028: # login -> LoginResponse
        payload_out = make_login_response(
            profile.name, profile.persona_id, profile.account_id
        )
        log.info("[+] Responding to login (0x0028) with LoginResponse")
        resp = make_blaze_response(0x0001, command, msg_id, payload_out)
        log.info("[LOGIN TX] msg=%d payload=%d frame=%d", msg_id, len(payload_out), len(resp))
        writer.write(resp)
        await writer.drain()
    elif command in (0x0098, 0x0032, 0x003C): # originLogin, silentLogin, expressLogin
        session_key = secrets.token_hex(16)
        payload_out = (b"\x00" if command == 0x0032 and MINIMAL_SILENT_LOGIN_RESPONSE
                       else make_full_login_response(
                           profile.name, profile.persona_id, profile.account_id,
                           session_key
                       ))
        log.info("[+] Responding to %s (0x%04X) with FullLoginResponse", cmd_name, command)
        resp = make_blaze_response(0x0001, command, msg_id, payload_out)
        log.info("[LOGIN TX] msg=%d payload=%d frame=%d", msg_id, len(payload_out), len(resp))
        user_authenticated = make_blaze_notification(
            0x7802, 0x0008,
            make_user_authenticated_notification(
                profile.persona_id, profile.account_id, profile.name, session_key
            )
        )
        # Complete the RPC callback before announcing authentication.  If both
        # frames arrive together, UserAuthenticated advances UserManager to 11
        # before the FullLoginResponse callback checks for state 10.  Let one
        # client tick elapse so the stock state machine processes them in order.
        writer.write(resp)
        await writer.drain()
        await asyncio.sleep(0.050)
        writer.write(user_authenticated)
        await writer.drain()
        log.info("[USERSESSIONS TX] UserAuthenticated payload=%d frame=%d",
                 len(user_authenticated) - 12, len(user_authenticated))
        session.authenticated = True
        previous = ACTIVE_SESSIONS.get(profile.persona_id)
        if previous is not None and previous is not session:
            log.warning("Replacing active session for %s id=%d",
                        profile.name, profile.persona_id)
        ACTIVE_SESSIONS[profile.persona_id] = session
    elif command == 0x00F2: # getEmailOptInSettings
        payload_out = make_email_opt_in_settings_response()
        resp = make_blaze_response(0x0001, command, msg_id, payload_out)
        log.info("[EMAIL OPT-IN TX] msg=%d payload=%d hex=%s",
                 msg_id, len(payload_out), payload_out.hex(" "))
        writer.write(resp)
        await writer.drain()
    elif command in (0x0029, 0x00F1): # acceptTos / acceptLegalDocs
        # Both generated RPCs have a void response.  A zero-byte payload is
        # required; a single TDF terminator is not an empty response object.
        resp = make_blaze_response(0x0001, command, msg_id, b"")
        log.info("[LEGAL ACCEPT TX] %s msg=%d", cmd_name, msg_id)
        writer.write(resp)
        await writer.drain()
    elif command in (0x00F6, 0x002F):
        doc_name = ("Terms of Service" if command == 0x00F6
                    else "Privacy Policy")
        content = (f"{doc_name}\n\n"
                   "Local private server. No account data is transmitted to Electronic Arts.")
        payload_out = make_legal_doc_content_response(content)
        resp = make_blaze_response(0x0001, command, msg_id, payload_out)
        log.info("[LEGAL DOC TX] %s msg=%d payload=%d hex=%s",
                 doc_name, msg_id, len(payload_out), payload_out.hex(" "))
        writer.write(resp)
        await writer.drain()
    else:
        log.warning("[!] Unhandled Auth command: %s (0x%04X)", cmd_name, command)
        resp = make_blaze_response(0x0001, command, msg_id, b"\x00")
        writer.write(resp)
        await writer.drain()

async def read_proxy_header_and_blaze_header(reader, transport_peer):
    """Accept an optional trusted HAProxy PROXY v1 line from local stunnel."""
    prefix = await reader.readexactly(6)
    if prefix != b"PROXY ":
        return prefix + await reader.readexactly(6), transport_peer

    remainder = await reader.readuntil(b"\r\n")
    line = prefix + remainder
    if len(line) > 108:
        raise ValueError("oversized PROXY protocol header")
    parts = line[:-2].decode("ascii", errors="strict").split()
    client_peer = transport_peer
    if len(parts) == 6 and parts[1] in ("TCP4", "TCP6"):
        try:
            client_peer = (parts[2], int(parts[4]))
        except ValueError as exc:
            raise ValueError("invalid PROXY source port") from exc
    elif len(parts) != 2 or parts[1] != "UNKNOWN":
        raise ValueError("invalid PROXY protocol header")
    return await reader.readexactly(12), client_peer


def make_stat_group_response(request_payload: bytes) -> bytes:
    # 0x00C1DAD0 indexes the group by the string at response+8.  Sending the
    # requested name in both candidate members keeps the group lookup valid.
    name = find_tdf_string(request_payload, "NAME", "")
    return tdf_string_field("CNAM", name) + tdf_string_field("NAME", name)


class LicensePlateStore:
    """Plate per persona.  The client restores it only from the stats view."""

    def __init__(self, path=None):
        self.path = Path(path) if path else STATE_DIR / "license_plates.json"
        self._plates = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._plates = {int(key): (int(value["id"]), str(value["tag"]))
                            for key, value in raw.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.warning("Unable to load license plates %s: %s", self.path, exc)

    def get(self, persona_id: int):
        return self._plates.get(persona_id)

    def set(self, persona_id: int, plate_id: int, tag: str):
        self._plates[persona_id] = (plate_id, tag)
        data = {str(key): {"id": value[0], "tag": value[1]}
                for key, value in sorted(self._plates.items())}
        try:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            log.warning("Unable to save license plates %s: %s", self.path, exc)


LICENSE_PLATES = LicensePlateStore()


def parse_license_plate_report(payload: bytes):
    """Return (id, tag) from a submitted license_plate game report, or None.

    The report maps the sender to its plate.  The map key is ignored: a client
    patched to always report id 1 sends that here, so the session decides.
    """
    # The report's variable-typed fields stop the top-level scanner before GTYP.
    if tdf_string_field("GTYP", "license_plate") not in payload:
        return None
    tag = encode_tag("PLAT", 0x05)
    at = payload.find(tag)
    if at < 0 or at + 6 >= len(payload) or payload[at + 4:at + 6] != b"\x00\x03":
        return None
    try:
        count, offset = decode_tdf_varint(payload, at + 6)
        if count != 1:
            return None
        _, offset = decode_tdf_varint(payload, offset)
        body = payload[offset:_skip_tdf_value(payload, offset, 0x03)]
    except ValueError:
        return None
    plate_tag = find_tdf_string(body, "TAG")
    return (find_tdf_int(body, "ID"), plate_tag) if plate_tag else None


def make_license_plate_stat_values(persona_id: int, plate) -> bytes:
    """StatValues with one entity: 0x006EE420 reads the id, then the tag."""
    plate_id, plate_tag = plate
    values = str(plate_id), plate_tag
    return (encode_tag("STAT", 0x04) + b"\x03" + tdf_varint(1) +
            tdf_int_field("EID", persona_id) +
            encode_tag("ETYP", 0x08) + tdf_varint(0) + tdf_varint(0) +
            tdf_int_field("POFF", 0) +
            encode_tag("STAT", 0x04) + b"\x01" + tdf_varint(len(values)) +
            b"".join(tdf_string(value) for value in values) +
            b"\x00\x00")


def make_stats_async_notification(request_payload: bytes, persona_id=None) -> bytes:
    """Complete a getStatsByGroupAsync view; only license_plate has stored rows."""
    name = find_tdf_string(request_payload, "NAME", "")
    # 0x00C1DC10 reads the license_plate view from this exact key and
    # 0x006EE445 dereferences the result.  Other stat handlers parse
    # "scope=value" keys, so they must keep an empty, unstored view here.
    key = "No_Scope_Defined" if name == "license_plate" else ""
    plate = (LICENSE_PLATES.get(persona_id)
             if name == "license_plate" and persona_id is not None else None)
    values = make_license_plate_stat_values(persona_id, plate) if plate else b"\x00"
    return (
        tdf_string_field("GRNM", name) +
        tdf_string_field("KEY", key) +
        tdf_int_field("LAST", 1) +
        encode_tag("STS", 0x03) + values +
        tdf_int_field("VID", find_tdf_int(request_payload, "VID", 0))
    )


BLAZE_IDLE_TIMEOUT = 300          # s without any frame (the game pings every 15 s)
MAX_BLAZE_PER_IP = int(os.environ.get("NFSMWU_MAX_CONN_PER_IP", "8"))
_BLAZE_PER_IP = {}


async def handle_blaze_client(reader, writer):
    transport_peer = writer.get_extra_info("peername")
    ip = transport_peer[0] if transport_peer else "?"
    if _BLAZE_PER_IP.get(ip, 0) >= MAX_BLAZE_PER_IP:
        log.warning("[LIMIT] %s already has %d Blaze connections: refused", ip, _BLAZE_PER_IP[ip])
        writer.close()
        return
    _BLAZE_PER_IP[ip] = _BLAZE_PER_IP.get(ip, 0) + 1
    try:
        await _handle_blaze_client(reader, writer)
    finally:
        _BLAZE_PER_IP[ip] -= 1
        if _BLAZE_PER_IP[ip] <= 0:
            _BLAZE_PER_IP.pop(ip, None)


async def _handle_blaze_client(reader, writer):
    transport_peer = writer.get_extra_info("peername")
    peer = transport_peer
    session = None
    log.info("NEW BLAZE TRANSPORT from %s", transport_peer)
    try:
        pending_header, peer = await asyncio.wait_for(
            read_proxy_header_and_blaze_header(reader, transport_peer), timeout=30)
        set_client_host(peer[0])
        profile = PLAYER_REGISTRY.get(peer[0])
        session = ClientSession(writer, transport_peer, peer, profile)
        log.info("NEW BLAZE CONNECTION client=%s transport=%s profile=%s/%d",
                 peer, transport_peer, profile.name, profile.persona_id)
        while True:
            if pending_header is not None:
                header = pending_header
                pending_header = None
            else:
                try:
                    header = await asyncio.wait_for(reader.readexactly(12), timeout=BLAZE_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    log.info("Blaze idle for %d s, closing %s", BLAZE_IDLE_TIMEOUT, peer)
                    break
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) > 0:
                        log.warning("Incomplete header (%d bytes) from %s:\n%s", len(e.partial), peer, hexdump(e.partial))
                    break

            length = (header[0] << 8) | header[1]
            component = (header[2] << 8) | header[3]
            command = (header[4] << 8) | header[5]
            error = (header[6] << 8) | header[7]
            qtype = (header[8] << 8) | header[9]
            msg_id = (header[10] << 8) | header[11]

            payload = b""
            if length > 0:
                payload = await asyncio.wait_for(reader.readexactly(length), timeout=60)

            log.info("[BLAZE RECV from %s]: Comp: 0x%04X, Cmd: 0x%04X, Type: 0x%04X, Id: 0x%04X, Len: %d",
                     peer, component, command, qtype, msg_id, length)
            if payload:
                log.info("[BLAZE RECV TDF] %04X/%04X %s", component, command, tdf_dump(payload))

            # 1. Redirector (0x0005) -> getServerList (0x0001) or getServerInstance
            if component == 0x0005:
                log.info("[+] Handling Redirector request from %s", peer)
                resp_payload = bytearray()

                # Tag ADDR (Union, type 0x06): union index 0 = IpAddress
                resp_payload.extend(encode_tag("ADDR", 0x06))
                resp_payload.append(0x00) # Union index 0

                resp_payload.extend(encode_tag("VALU", 0x03))
                # Group IpAddress:
                resp_payload.extend(encode_tag("HOST", 0x01))
                resp_payload.extend(encode_string(client_host()))

                resp_payload.extend(encode_tag("IP", 0x00))
                resp_payload.extend(encode_varint(host_ipv4(client_host())))

                resp_payload.extend(encode_tag("PORT", 0x00))
                resp_payload.extend(encode_varint(42127))

                resp_payload.append(0x00) # End of Group

                # Wii U: the patched client connects without ProtoSSL, so the main server is insecure too.
                log.info("[REDIRECTOR] request %s", tdf_dump(payload))
                resp_payload.extend(encode_tag("SECU", 0x00))
                resp_payload.extend(encode_varint(0))

                resp_payload.extend(encode_tag("XDNS", 0x00))
                resp_payload.extend(encode_varint(0))

                resp_payload.append(0x00) # End of struct

                resp = make_blaze_response(component, command, msg_id, bytes(resp_payload))
                log.info("[+] Sending Redirector Response (%d bytes)", len(resp))
                writer.write(resp)
                await writer.drain()

            # 2. Authentication (0x0001)
            elif component == 0x0001:
                await handle_auth_request(session, command, msg_id, payload, peer)

            # 3. Util (0x0009)
            elif component == 0x0009:
                log.info("[+] Handling Util request (cmd 0x%04X) from %s", command, peer)
                if command == 0x0007: # preAuth
                    payload_out = make_preauth_cids()
                    response = make_blaze_response(
                        component=0x0009,
                        command=0x0007,
                        msg_id=msg_id,
                        payload=payload_out
                    )
                    log.info(
                        "[PREAUTH TX] msg=%d payload=%d frame=%d hex=%s",
                        msg_id, len(payload_out), len(response), response.hex(" ")
                    )
                    writer.write(response)
                    await writer.drain()
                elif command == 0x0002: # ping
                    resp = make_blaze_response(component, command, msg_id, b"")
                    writer.write(resp)
                    await writer.drain()
                elif command == 0x0008: # postAuth
                    payload_out = make_post_auth_response(session.profile.account_id)
                    resp = make_blaze_response(component, command, msg_id, payload_out)
                    user_added = make_blaze_notification(
                        0x7802, 0x0002,
                        make_user_added_notification(
                            session.profile.persona_id,
                            session.profile.account_id,
                            session.profile.name,
                        )
                    )
                    extended_data_update = make_blaze_notification(
                        0x7802, 0x0001, make_user_extended_data_update_notification(
                            user_id=session.profile.persona_id
                        )
                    )
                    log.info("[POSTAUTH TX] msg=%d payload=%d hex=%s",
                             msg_id, len(payload_out), resp.hex(" "))
                    writer.write(resp)
                    writer.write(user_added)
                    writer.write(extended_data_update)
                    await writer.drain()
                    log.info("[USERSESSIONS TX after postAuth] UserAdded payload=%d frame=%d",
                             len(user_added) - 12, len(user_added))
                    log.info("[USERSESSIONS TX after postAuth] ExtendedDataUpdate payload=%d frame=%d",
                             len(extended_data_update) - 12, len(extended_data_update))
                    await announce_friend_presence(session)
                elif command == 0x0001: # fetchClientConfig
                    cfid = parse_cfid(payload)
                    log.info("[+] fetchClientConfig requested for section: '%s'", cfid)
                    if cfid == "Url":
                        payload_out = make_fetch_config_response(config_for_client(URL_CONFIG))
                    elif cfid == "Game":
                        payload_out = make_fetch_config_response(config_for_client(GAME_CONFIG))
                    else:
                        log.warning("Unknown config section '%s', returning empty map", cfid)
                        payload_out = make_fetch_config_response({})
                    resp = make_blaze_response(component, command, msg_id, payload_out)
                    log.info("[FETCH CONFIG TX] section=%s msg=%d payload=%d hex=%s",
                             cfid, msg_id, len(payload_out), resp.hex(" "))
                    writer.write(resp)
                    await writer.drain()
                elif command == 0x0014: # filterForProfanity
                    payload_out = make_filter_user_text_response(payload)
                    resp = make_blaze_response(component, command, msg_id, payload_out)
                    log.info("[FILTER TEXT TX] msg=%d request_hex=%s response_hex=%s",
                             msg_id, payload.hex(" "), payload_out.hex(" "))
                    writer.write(resp)
                    await writer.drain()
                elif command == 0x0015: # fetchQosConfig
                    payload_out = make_qos_config_info()
                    resp = make_blaze_response(component, command, msg_id, payload_out)
                    log.info("[QOS CONFIG TX] msg=%d payload=%d hex=%s",
                             msg_id, len(payload_out), resp.hex(" "))
                    writer.write(resp)
                    await writer.drain()
                else: # other util commands
                    log.info("[UTIL GENERIC] cmd=0x%04X payload=%d hex=%s",
                             command, len(payload), payload.hex(" "))
                    resp = make_blaze_response(component, command, msg_id, b"")
                    writer.write(resp)
                    await writer.drain()

            # 4. GameManager (0x0004)
            elif component == 0x0004:
                await handle_game_manager_request(
                    session, command, msg_id, payload, peer
                )

            elif component == 0x7802 and command in (0x0014, 0x0008):
                # updateNetworkInfo/updateHardwareFlags are acknowledged first,
                # then Blaze publishes the resulting session data back to the
                # client. The startup flow waits for this notification.
                if command == 0x0014:
                    addr, qdat = parse_network_info(payload)
                    if addr is not None and qdat is not None:
                        session.network_addr = addr
                        session.qdat = qdat
                        log.info("[USERSESSIONS] captured ADDR=%d QDAT=%d",
                                 len(session.network_addr), len(session.qdat))
                    else:
                        log.warning("[USERSESSIONS] malformed updateNetworkInfo payload")
                elif len(payload) >= 5 and payload[:4] == encode_tag("HWFG", 0x00):
                    session.hardware_flag = payload[4] & 0x3f

                resp = make_blaze_response(component, command, msg_id, b"")
                session_update = make_blaze_notification(
                    0x7802, 0x0001,
                    make_user_extended_data_update_notification(
                        session.network_addr, session.qdat,
                        session.hardware_flag, session.profile.persona_id
                    )
                )
                log.info("[USERSESSIONS TX after cmd 0x%04X] response=%d "
                         "session_update_payload=%d request_hex=%s",
                         command, len(resp), len(session_update) - 12,
                         payload.hex(" "))
                writer.write(resp)
                writer.write(session_update)
                await writer.drain()

            elif component == 0x0019 and command == 0x0006:
                payload_out = make_association_lists_response(
                    payload, session.profile.persona_id,
                    PLAYER_REGISTRY.all_profiles(),
                )
                log.info("[ASSOCIATION LISTS TX] getLists msg=%d request=%d "
                         "request_hex=%s response_hex=%s",
                         msg_id, len(payload), payload.hex(" "), payload_out.hex(" "))
                resp = make_blaze_response(component, command, msg_id, payload_out)
                writer.write(resp)
                await writer.drain()

            elif component == 0x0007 and command == 0x0004:
                payload_out = make_stat_group_response(payload)
                log.info("[STATS TX] getStatGroup msg=%d request_hex=%s response_hex=%s",
                         msg_id, payload.hex(" "), payload_out.hex(" "))
                writer.write(make_blaze_response(component, command, msg_id, payload_out))
                await writer.drain()

            elif component == 0x0007 and command == 0x0010:
                # getStatsByGroupAsync returns its rows through notification
                # 50 and the client's stat jobs wait for LAST on the same VID.
                notification = make_stats_async_notification(
                    payload, session.profile.persona_id
                )
                log.info("[STATS TX] getStatsByGroupAsync msg=%d request_hex=%s "
                         "notification_hex=%s", msg_id, payload.hex(" "),
                         notification.hex(" "))
                writer.write(make_blaze_response(component, command, msg_id, b""))
                writer.write(make_blaze_notification(component, 0x0032, notification))
                await writer.drain()

            elif component == 0x7802 and command == 0x000C:
                # lookupUser(UserIdentification) -> UserData {EDAT, FLGS, USER}. The Wii U host does this for every
                # player the server announces (friend/association update); an empty reply is dereferenced as a
                # null user at 0x02c08040 and the game crashes (seen with two Cemus).
                payload_out = make_lookup_user_response(payload)
                writer.write(make_blaze_response(component, command, msg_id, payload_out))
                await writer.drain()
                log.info("[USERSESSIONS TX] lookupUser %s", tdf_dump(payload_out, 300))

            elif component == 0x001C and command in (0x0001, 0x0064, 0x0065):
                # submitGameReport / submitTrustedMid/EndGameReport: ack, then ResultNotification (0x1C/0x72)
                # to everyone in the reporter's game (see make_result_notification).
                log.info("[GAMEREPORT] %s cmd=0x%04X from %s: %s", session.profile.name, command, peer,
                         tdf_dump(payload, 3000))
                writer.write(make_blaze_response(component, command, msg_id, b""))
                await writer.drain()
                await send_result_notification(session, payload)

            else:
                if component == 0x001C and command == 0x0002:
                    plate = parse_license_plate_report(payload)
                    if plate is not None:
                        LICENSE_PLATES.set(session.profile.persona_id, *plate)
                        log.info("[LICENSE PLATE] player=%d id=%d tag=%s",
                                 session.profile.persona_id, plate[0], plate[1])
                    log.info("[GAMEREPORT] offline %s: %s", session.profile.name, tdf_dump(payload, 3000))
                    # The Wii U sends the speedlist event report (Online::MultiplayerReport, RSLT/STAT per
                    # player) through submitOfflineGameReport too; it waits for the results like the PC does.
                    if report_class_id(payload) == 0xc4bd435a:
                        writer.write(make_blaze_response(component, command, msg_id, b""))
                        await writer.drain()
                        await send_result_notification(session, payload)
                        continue
                log.info("[!] Generic reply for comp 0x%04X, cmd 0x%04X payload=%d hex=%s from %s",
                         component, command, len(payload), payload.hex(" "), peer)
                # A successful Blaze RPC with no response TDF has a genuinely
                # empty payload.  A lone zero byte is not an empty struct and
                # makes several client decoders reject the response.
                resp = make_blaze_response(component, command, msg_id, b"")
                writer.write(resp)
                await writer.drain()

    except Exception as e:
        log.error("Blaze error from %s: %s", peer, e)
        traceback.print_exc()
    finally:
        log.info("Blaze disconnected: %s", peer)
        if session is not None and session.game_id:
            await detach_session_from_game(session, reason=2)
        if session is not None and RELAY_ENABLED:
            RELAY.release(session.profile.persona_id)
        if (session is not None and
                ACTIVE_SESSIONS.get(session.profile.persona_id) is session):
            ACTIVE_SESSIONS.pop(session.profile.persona_id, None)
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass

async def handle_https_client(reader, writer):
    peer = writer.get_extra_info("peername")
    set_client_host(peer[0])
    log.info("NEW HTTPS CONNECTION from %s", peer)
    try:
        data = await reader.read(4096)
        try:
            log.info("HTTPS REQUEST from %s:\n%s", peer, data.decode("utf-8", errors="replace"))
        except:
            log.info("HTTPS REQUEST from %s:\n%s", peer, hexdump(data))

        xml_body = (
            '<?xml version="1.0" encoding="UTF-8"?>\r\n'
            '<serverlist>\r\n'
            '  <server>\r\n'
            '    <name>nfs13-pc</name>\r\n'
            f'    <address>{client_host()}</address>\r\n'
            '    <port>42127</port>\r\n'
            '    <secure>true</secure>\r\n'
            '  </server>\r\n'
            '</serverlist>\r\n'
        )

        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/xml\r\n"
            f"Content-Length: {len(xml_body)}\r\n"
            "Connection: close\r\n"
            "\r\n" + xml_body
        )
        writer.write(response.encode("utf-8"))
        await writer.drain()
    except Exception as e:
        log.error("HTTPS error from %s: %s", peer, e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass


def make_most_wanted_list_body(profile: PlayerProfile, points=26215) -> bytes:
    """Pagerunner notificationcentre model consumed by MostWantedListManager."""
    # These names are taken from the retail client's embedded cached response
    # template.  In particular, this endpoint uses lower-case row members even
    # though many neighbouring Pagerunner models use title-case JSON names.
    model = {
        "IsCachedData": True,
        "LocalUserPosition": 1,
        "LocalUserPoints": points,
        "LocalUserProfilePic": "",
        "MostWantedList": [{
            "id": profile.persona_id,
            "total_bounty": f"{points:,}".replace(",", " "),
            "origin_name": profile.name,
            "position": 1,
            "raw_total_bounty": points,
            "uid": profile.account_id,
            "profile_pic": "",
            "isLocal": True,
            "rich_presence": {
                "onlineStatus": 0,
                "lastPlatform": "",
                "richPresenceItems": [],
            },
        }],
        "ListTarget": "",
        "LastAward": "",
    }
    return json.dumps(model, separators=(",", ":")).encode("utf-8")


def make_top_speedwall_body(query) -> bytes:
    """Return the retail event_info map keyed by the decimal event id.

    The client treats ``info`` as an object, iterates its member names, and
    parses each name with ``strtol``.  An array (or event0/event1 members) is
    rejected before any speedwall row is read.
    """
    info = {}
    for key in sorted(
            (name for name in query if name.startswith("event") and
             name[5:].isdigit()),
            key=lambda name: int(name[5:])):
        try:
            event_id = int(query[key][0])
        except (IndexError, TypeError, ValueError):
            event_id = 0
        info[str(event_id)] = {
            "No1ScoreToBeat": 0,
            "No1TimeToBeatMS": 0,
        }
    return json.dumps({"info": info}, separators=(",", ":")).encode("utf-8")


# NFS13.lab.exe 0x00693467 requires onlineProgression to be a JSON object.
# 0x0068A650 requires an item whose A is each DLC vehicle and whose E resolves
# to a record with GameChangerID 2115001 (GAMEPLAY.BNDL resource 0x002045B9).
ONLINE_PROGRESSION_VEHICLES = (2076005, 2076070, 2076137, 2076203, 2076266)
ONLINE_PROGRESSION_GAME_CHANGER = 2115001
AUTOLOG_BASE_QUERY_KEYS = frozenset((
    "session", "serviceName", "platform", "language", "version", "flavour",
    "pName",
))
# Wii U Autolog login (0x0266cea0): GET /?session=<getAuthToken>&platform=CAFE&serviceName=GAMEWIIU&version=080
# &logMeIn=true
AUTOLOG_WIIU_LOGIN_KEYS = frozenset(("session", "platform", "serviceName", "version", "logMeIn"))


def make_online_progression_body() -> bytes:
    return json.dumps({
        "status": "ok",
        # 0x006A1760 and 0x006FE070 fail their jobs unless these objects exist.
        "Patches": {},
        "milestonePatches": {},
        "onlineProgression": {
            str(vehicle_id): {
                "A": vehicle_id,
                "E": ONLINE_PROGRESSION_GAME_CHANGER,
            }
            for vehicle_id in ONLINE_PROGRESSION_VEHICLES
        },
    }, separators=(",", ":")).encode("utf-8")


def make_wiiu_autolog_login_body(query: dict) -> bytes:
    """Wii U login HSM (state table 0x100365d8). Both requests read the reply into a 32-byte buffer at
    hsm+0x90 (size set in 0x0266cea0 / 0x0266d22c): a longer body fails the step and the game goes offline.
    TimeStampPostLogin (GET ...&logMeIn=true, reply 0x0266e240) reads int CollectablesRequestTime;
    OnlinePassKillSwitch (GET ?session&serviceName&version, reply 0x0266e3a0) reads bool PhotoTitles with no
    null check (a missing key crashes)."""
    if query.get("logMeIn") == ["true"]:
        return b'{"CollectablesRequestTime":600}'
    return b'{"PhotoTitles":true}'


def make_rename_body(client_ip: str, query: dict) -> bytes:
    """Rename the caller, identified by its address; applies on next login."""
    name = query.get("name", [""])[0]
    error = PLAYER_REGISTRY.rename(client_ip, name)
    result = ({"status": "error", "message": error} if error else
              {"status": "ok", "name": name,
               "message": "applies after the next online login"})
    return json.dumps(result).encode("utf-8") + b"\n"


# Event ids the client lists together in its GAMEMODES resource and requests
# from Autolog.  0x006E9FA0 skips unknown ids and has room for only 10 events.
# Online events from the client table at 0x006E5DC0, as logged by the trace hook.
# 390703, 1113775 (the freeroam event) and 1335448/1335971 are left out.
SPEEDLIST_EVENT_POOL = (
    1430729, 1430756, 1430796, 1430801, 1430830, 1430876, 1430928, 1430941,
    1430954, 1430998, 1463004, 1463179, 1463185, 1463196, 1463201, 1463357,
    1463373, 1463417, 1463475, 1463491, 1463540, 1463562, 1463687, 1463703,
    1463715, 1463730, 1463738, 1463753, 1463935, 1552017, 1552082, 1552127,
    1552146, 1552209, 1552276, 1552302, 1552315, 1552321, 1552359, 1552388,
    1552406, 1552443, 1552460, 1552507, 1552517, 1552530, 1552554, 1552785,
    1552829, 1552840, 1592011, 1592177, 1592209, 1592255, 1592280, 1592584,
    1592599, 1592612, 1592941, 1632299, 1632305, 1632358, 1632384, 1632400,
    1632468, 1632798, 1632813, 1632831, 1632839, 1632936, 1632964, 1655109,
    1655159, 1655194, 1655209, 1655233, 1655254, 2194064, 2194086, 2194112,
    2194133, 2194197, 2194471, 2258001, 2258010,
)
SPEEDLIST_LENGTH = 5


def pick_speedlist_events(previous=(), rng=random):
    """Random events for one speedlist, avoiding the ones the client just played."""
    fresh = [event_id for event_id in SPEEDLIST_EVENT_POOL if event_id not in previous]
    if len(fresh) < SPEEDLIST_LENGTH:
        fresh = list(SPEEDLIST_EVENT_POOL)
    return rng.sample(fresh, SPEEDLIST_LENGTH)


def make_playlist_body(action: str, previous=()) -> bytes:
    """GetNextPlaylist/GetVotePlaylists objects as parsed by 0x006EB0D0."""
    def playlist(asset_id, name, events):
        return {"asset_id": asset_id, "name": name,
                "events": [{"event_id": event_id, "restriction_id": 0}
                           for event_id in events]}
    if action == "GetVotePlaylists":
        return json.dumps({
            "VoteA": playlist(1, "Private A", pick_speedlist_events(previous)),
            "VoteB": playlist(2, "Private B", pick_speedlist_events(previous)),
        }, separators=(",", ":")).encode("utf-8")
    return json.dumps({"Playlist": playlist(1, "Community", pick_speedlist_events(previous))},
                      separators=(",", ":")).encode("utf-8")


async def handle_autolog_http(reader, writer):
    peer = writer.get_extra_info("peername")
    set_client_host(peer[0])
    try:
        header_block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        header_lines = header_block[:-4].split(b"\r\n")
        first_line = header_lines[0].decode('latin1', errors='replace')
        content_length = 0
        for line in header_lines[1:]:
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip() or b"0")
                break
        if content_length < 0 or content_length > 256 * 1024:
            log.warning("[AUTOLOG HTTP] body of %d bytes from %s refused", content_length, peer)
            return
        body_in = (await asyncio.wait_for(reader.readexactly(content_length), timeout=30)
                   if content_length else b"")
        log.info(
            "[AUTOLOG HTTP] %s from %s headers=%s body_len=%d body=%s",
            first_line,
            peer,
            header_block.decode('latin1', errors='replace').replace("\r\n", " | "),
            len(body_in),
            body_in.decode('utf-8', errors='replace'),
        )
        method, target, _ = first_line.split(" ", 2)
        path, separator, raw_query = target.partition("?")
        query = parse_qs(raw_query, keep_blank_values=True)
        if method == "GET" and not separator and path in ("/", "/server_manifest"):
            # Bootstrap schema consumed by NFS13's Autolog JSON parser.  The
            # misspelling HTTPTimout is intentional and matches the retail
            # client field name.
            body = json.dumps({
                "serverActive": True,
                "allow": {
                    "postPhotos": True,
                    "uploadFrequency": 30.0,
                    "uploadSessionFrequency": 30.0,
                    "HTTPTimout": 30000,
                    "HTTPRetry": 3,
                },
                "bundles": {},
            }, separators=(",", ":")).encode("utf-8")
        elif (method == "GET" and path == "/view_model" and
              query.get("action") == ["GetData"] and
              query.get("getComponent") == ["MainMap"]):
            # LiveMapManager reads the recommendation availability from the
            # MainMap model.  Without this flag the EasyDrive row is rendered
            # but remains a non-enterable empty-state, even when the detailed
            # autolog_recommends endpoint itself is valid.
            body = json.dumps({
                "HasRecommendations": True,
                "Items": [{"EventId": 462174}],
            }, separators=(",", ":")).encode("utf-8")
        elif (method == "GET" and path == "/view_model" and
              query.get("getComponent") == ["friend_recommends"]):
            # LiveMapManager loads this lightweight component as soon as the
            # city becomes active.  It uses Items[].EventId to decide whether
            # the EasyDrive "Autolog Recommends" tier is enterable.  Returning
            # only a generic status object leaves HasRecommendations false and
            # makes the highlighted menu item ignore EASY_RIGHT.
            body = json.dumps({
                "HasRecommendations": True,
                "Items": [{"EventId": 462174}],
            }, separators=(",", ":")).encode("utf-8")
        elif (method == "GET" and path == "/view_model" and
              query.get("action") == ["Index"] and
              query.get("getComponent") == ["autolog_recommends"]):
            # Minimal but real Pagerunner view model.  The NFS13 callback
            # requires an Items array whose entries contain EventId,
            # vehicleID and Gamertag.  Use an event and the current car that
            # are present in the stock game's first unlocked set.  Additional
            # fields are harmless to the JSON model and make the response
            # useful to the neighbouring recommendation/detail callbacks.
            # The callback rejects a recommendation whose vehicleID is a
            # different non-zero vehicle than the request's currentVehicle.
            # Build the item from the request so it remains valid after the
            # player changes cars.  The unlocked event list is authoritative
            # for the current profile, so prefer its first event as well.
            try:
                current_vehicle = int(query.get("currentVehicle", ["0"])[0])
            except (TypeError, ValueError):
                current_vehicle = 0
            try:
                event_id = int(query.get("unlockedEvents", ["462174"])[0].split(",", 1)[0])
            except (TypeError, ValueError):
                event_id = 462174
            body = json.dumps({
                "HasRecommendations": True,
                "Items": [{
                    "typeIndex": 0,
                    # The summary callback totals Items[].SP without a null
                    # check (NFS13.lab.exe 0x00722618).  It must therefore be
                    # present as a JSON integer even for a local placeholder.
                    "SP": 0,
                    "EventId": event_id,
                    "vehicleID": current_vehicle,
                    # Recommendations authored by the local persona are
                    # discarded by the client as self-recommendations.
                    "Gamertag": "LocalRival",
                    "awardID": 0,
                    "IsBiggerBetter": False,
                    "HasLocalPlayerDoneEvent": True,
                    "LocalPlayerValue": 0,
                    "menuTitle": "Local Autolog",
                    "speedwall": {"Items": []},
                }],
                "hasFriends": True,
            }, separators=(",", ":")).encode("utf-8")
        elif (method == "GET" and path == "/view_model" and
              query.get("action") == ["GetMostWantedList"] and
              query.get("getComponent") == ["notificationcentre"]):
            body = make_most_wanted_list_body(PLAYER_REGISTRY.get(peer[0]))
        elif (method == "GET" and path == "/view_model" and
              query.get("action") == ["TopOfSpeedwall"] and
              query.get("getComponent") == ["event_info"]):
            body = make_top_speedwall_body(query)
        elif (method == "GET" and path == "/view_model" and
              query.get("getComponent") == ["playlist"] and
              query.get("action", [""])[0] in ("GetNextPlaylist", "GetVotePlaylists")):
            previous = {int(values[0]) for key, values in query.items()
                        if key.startswith("prev") and values[0].isdigit()}
            body = make_playlist_body(query["action"][0], previous)
        elif method == "GET" and path == "/rename":
            body = make_rename_body(peer[0], query)
        elif (method == "GET" and path == "/" and query.get("serviceName") == ["GAMEWIIU"] and
              set(query) <= AUTOLOG_WIIU_LOGIN_KEYS):
            body = make_wiiu_autolog_login_body(query)
        elif (method == "GET" and path == "/" and set(query) == AUTOLOG_BASE_QUERY_KEYS):
            body = make_online_progression_body()
        else:
            body = b'{"status":"ok"}'
        resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(resp)
        await writer.drain()
        log.info("[AUTOLOG TX] %s %s -> %d bytes: %s", method, path, len(body),
                 body[:300].decode("utf-8", "replace"))
    except Exception as e:
        log.error("Autolog HTTP error from %s: %s", peer, e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass

class QosUdpProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        log.info("QoS UDP Server listening on port 17502")

    def datagram_received(self, data, addr):
        log.info("QoS UDP Packet from %s (%d bytes):\n%s", addr, len(data), hexdump(data))
        if len(data) < 20:
            log.warning("QoS UDP probe from %s is too short (%d bytes)", addr, len(data))
            return

        request_id = int.from_bytes(data[4:8], "big")
        response_header = bytearray(data[:20])
        if request_id >= 2 and len(data) > 20:
            probe_index = int.from_bytes(data[12:16], "big")
            probe_count = int.from_bytes(data[16:20], "big")
            if probe_count == 0 or probe_index + 1 < probe_count:
                log.info("QoS UDP bandwidth probe %d/%d accepted from %s",
                         probe_index + 1, probe_count, addr)
                return

            # A bandwidth request is a burst numbered 0..N-1. DirtySDK's
            # completion path compares this field with XML <numprobes>, so the
            # coordinator acknowledges the final burst with the received count.
            response_header[12:16] = probe_count.to_bytes(4, "big")

        # DirtySDK sends a 20-byte probe header and expects the QoS service to
        # append the public address it observed plus a zero-length reply body:
        #   probe[20] + observed IPv4[4] + observed port[2] + body length[4]
        # The client uses those extra fields both to validate the response and
        # to determine its externally visible address.
        try:
            observed_ip = bytes(int(part) for part in addr[0].split("."))
            if len(observed_ip) != 4:
                raise ValueError("not an IPv4 address")
        except (TypeError, ValueError):
            log.warning("QoS UDP probe has unsupported source address %r", addr)
            return

        response = bytes(response_header) + observed_ip + int(addr[1]).to_bytes(2, "big") + b"\x00\x00\x00\x00"
        self.transport.sendto(response, addr)
        log.info("QoS UDP Reply to %s (%d bytes):\n%s", addr, len(response), hexdump(response))


async def handle_qos_http(reader, writer):
    """DirtySDK QoS coordinator served over TCP on the UDP probe port."""
    peer = writer.get_extra_info("peername")
    set_client_host(peer[0])
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        request_line = request.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parts = request_line.split(" ")
        target = parts[1] if len(parts) >= 2 else "/"
        parsed = urlsplit(target)
        query = parse_qs(parsed.query)
        log.info("[QOS HTTP] %s from %s", request_line, peer)

        server_ip = int.from_bytes(bytes((192, 168, 1, 110)), "big")
        if parsed.path.endswith("/qos/qos"):
            qtype = int(query.get("qtyp", ["0"])[0] or 0)
            if qtype == 1:
                num_probes, probe_size, request_id, request_secret = 0, 0, 1, 0
            else:
                # DirtySDK selects the latency or bandwidth response parser by
                # request id: ids below 2 are latency, ids 2+ are bandwidth.
                num_probes, probe_size, request_id, request_secret = 4, 1200, 2, 1
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<qos>'
                f'<numprobes>{num_probes}</numprobes>'
                '<qosport>17502</qosport>'
                f'<probesize>{probe_size}</probesize>'
                f'<qosip>{server_ip}</qosip>'
                f'<requestid>{request_id}</requestid>'
                f'<reqsecret>{request_secret}</reqsecret>'
                '</qos>'
            ).encode("ascii")
        elif parsed.path.endswith("/qos/firetype"):
            body = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<firetype><firetype>2</firetype></firetype>')
        elif parsed.path.endswith("/qos/firewall"):
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<firewall>'
                f'<ips><ips>{server_ip}</ips></ips>'
                '<numinterfaces>1</numinterfaces>'
                '<ports><ports>17502</ports></ports>'
                '<requestid>1</requestid><reqsecret>1</reqsecret>'
                '</firewall>'
            ).encode("ascii")
        else:
            body = b""

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/xml\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        writer.write(response)
        await writer.drain()
        log.info("[QOS HTTP TX] path=%s bytes=%d body=%s",
                 parsed.path, len(body), body.decode("ascii", "replace"))
    except Exception as exc:
        log.warning("[QOS HTTP] error from %s: %s", peer, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def double_sp_watch():
    """When a Double SP window opens or closes, push fresh extended data (DMAP 0x70004) to everyone online."""
    active = double_sp_seconds_left() > 0
    log.info("[DOUBLE SP] schedule %s: %s (%d s left)", os.environ.get("NFSMWU_DOUBLE_SP", "weekend"),
             "ON" if active else "off", double_sp_seconds_left())
    while True:
        await asyncio.sleep(60)
        now_active = double_sp_seconds_left() > 0
        if now_active == active:
            continue
        active = now_active
        log.info("[DOUBLE SP] window %s; updating %d players", "opened" if active else "closed", len(ACTIVE_SESSIONS))
        for s in list(ACTIVE_SESSIONS.values()):
            try:
                await send_notification(s, 0x7802, 0x0001, make_user_extended_data_update_notification(
                    s.network_addr, s.qdat, s.hardware_flag, s.profile.persona_id))
            except Exception as exc:  # noqa: BLE001
                log.warning("[DOUBLE SP] update to %s failed: %s", s.profile.name, exc)


async def main():
    loop = asyncio.get_running_loop()
    asyncio.get_running_loop().create_task(double_sp_watch())

    # Wii U: plain TCP straight from the (patched) game, no stunnel frontend.
    blaze_server = await asyncio.start_server(handle_blaze_client, BIND_HOST, 42127)
    log.info("Blaze server (redirector + main) listening on %s:42127, plain TCP", BIND_HOST)

    https_server = await asyncio.start_server(handle_https_client, BIND_HOST, 44125)
    log.info("HTTP server listening on %s:44125 (plain)", BIND_HOST)

    autolog_server = await asyncio.start_server(handle_autolog_http, BIND_HOST, 17501)
    log.info("Autolog HTTP mock server listening on %s:17501", BIND_HOST)

    qos_http_server = await asyncio.start_server(handle_qos_http, BIND_HOST, 17502)
    log.info("QoS HTTP coordinator listening on %s TCP port 17502", BIND_HOST)

    await loop.create_datagram_endpoint(lambda: QosUdpProtocol(), local_addr=(BIND_HOST, 17502))

    if RELAY_ENABLED:
        opened = 0
        for port in RELAY.ports:
            try:
                await loop.create_datagram_endpoint(lambda p=port: _RelayPort(p), local_addr=(BIND_HOST, port))
                opened += 1
            except OSError as exc:
                RELAY.free.remove(port)
                log.warning("[RELAY] UDP port %d unavailable: %s", port, exc)
        log.info("[RELAY] race traffic relayed through UDP %s (%d ports open) - forward these on the router",
                 _RELAY_RANGE, opened)
    else:
        log.info("[RELAY] off: players connect to each other directly (NFSMWU_RELAY=on to relay)")
    log.info("Addresses handed out: LAN %s, internet %s", SERVER_HOST, PUBLIC_HOST or "(none: LAN only)")

    async with blaze_server, https_server, autolog_server, qos_http_server:
        await asyncio.gather(
            blaze_server.serve_forever(),
            https_server.serve_forever(),
            autolog_server.serve_forever(),
            qos_http_server.serve_forever()
        )

if __name__ == "__main__":
    asyncio.run(main())
