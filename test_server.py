# Copyright (C) 2026 sn0wd0wn (gregel, Dantiox, Alexes)
# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import server


class FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data += data

    async def drain(self):
        return None


class GameManagerEncodingTests(unittest.TestCase):
    def setUp(self):
        server.ACTIVE_GAMES.clear()
        server.ACTIVE_SESSIONS.clear()
        server.NEXT_GAME_ID = 1
        server.NEXT_MATCHMAKING_ID = 1
        server.NEXT_GAME_LIST_ID = 1

    def test_start_matchmaking_response_contains_session_id(self):
        payload = server.make_start_matchmaking_response(1234)
        self.assertEqual(server.find_tdf_int(payload, "MSID"), 1234)
        self.assertTrue(payload.endswith(b"\x00"))

    def test_join_game_response_contains_required_fields(self):
        payload = server.make_join_game_response(77, join_state=0)
        self.assertEqual(server.find_tdf_int(payload, "GID"), 77)
        self.assertEqual(server.find_tdf_int(payload, "JGS", -1), 0)

    def test_matchmaking_config_has_all_generated_response_members(self):
        payload = server.make_matchmaking_config_response()
        self.assertIn(server.encode_tag("GLST", 0x04), payload)
        self.assertIn(server.encode_tag("PPSR", 0x03), payload)
        self.assertIn(server.encode_tag("PDRC", 0x03), payload)
        self.assertIn(server.encode_tag("POSV", 0x04), payload)
        self.assertIn(server.encode_tag("RLST", 0x04), payload)
        self.assertTrue(payload.endswith(b"\x00"))

    def test_game_browser_encodes_active_game_and_roster(self):
        host = server.ClientSession(
            object(), ("127.0.0.1", 1), ("192.168.1.27", 5000),
            server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
        )
        request = (server.tdf_string_field("GNAM", "Alex Open Game") +
                   server.tdf_string_field("GVER", "Retail-0.9") +
                   server.tdf_int_field("PMAX", 8) +
                   server.tdf_int_field("GSET", 0x11F) + b"\x00")
        game = server.create_game_record(host, request, connected_host=True)

        full = server.make_list_games_response()
        browser = server.make_game_browser_data_list()
        update = server.make_game_list_update(7, games=(game,))

        for payload in (full, browser, update):
            self.assertIn(server.tdf_int_field("GID", game.game_id), payload)
            self.assertIn(server.tdf_string_field("GNAM", "Alex Open Game"), payload)
            self.assertIn(server.tdf_string_field("NAME", "Alex"), payload)
        self.assertEqual(server.find_tdf_int(update, "GLID"), 7)
        self.assertEqual(server.find_tdf_int(update, "DONE"), 1)

    def test_game_list_snapshot_sends_response_then_async_update(self):
        async def run_test():
            writer = FakeWriter()
            host = server.ClientSession(
                writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
                authenticated=True,
            )
            server.ACTIVE_SESSIONS[host.profile.persona_id] = host
            server.create_game_record(
                host,
                server.tdf_string_field("GNAM", "Browser Test") + b"\x00",
                connected_host=True,
            )
            writer.data.clear()
            await server.handle_game_manager_request(
                host, 0x0064, 44, b"\x00", host.client_peer
            )
            return host, bytes(writer.data)

        host, data = asyncio.run(run_test())
        self.assertEqual(host.game_list_id, 1)
        self.assertFalse(host.game_list_subscription)
        self.assertIn(b"\x00\x04\x00\x64\x00\x00\x10\x00\x00\x2c", data)
        self.assertIn(b"\x00\x04\x00\xc9\x00\x00\x20\x00", data)
        self.assertIn(server.tdf_string_field("GNAM", "Browser Test"), data)

    def test_top_level_scanner_preserves_matchmaking_fields(self):
        attrs = (server.encode_tag("ATTR", 0x05) + b"\x01\x01\x01" +
                 server.tdf_string("mode") + server.tdf_string("open"))
        payload = (attrs + server.tdf_string_field("GVER", "nfs-test") +
                   server.tdf_int_field("PMAX", 4) + b"\x00")
        self.assertEqual(server.find_tdf_field(payload, "ATTR", 0x05), attrs)
        self.assertEqual(server.find_tdf_string(payload, "GVER"), "nfs-test")
        self.assertEqual(server.find_tdf_int(payload, "PMAX"), 4)

    def test_retail_matchmaking_capture_decodes_and_builds_setup(self):
        capture = bytes.fromhex(
            (Path(__file__).parent / "fixtures" /
             "start_matchmaking_retail.hex").read_text(encoding="ascii")
        )
        self.assertEqual(len(capture), 523)
        self.assertEqual(server.find_tdf_string(capture, "GVER"), "Retail-0.9")
        self.assertEqual(server.find_tdf_int(capture, "PMAX"), 12)
        self.assertEqual(server.find_tdf_int(capture, "GSET"), 0x51F)
        self.assertEqual(server.find_tdf_int(capture, "NTOP"), 0x84)
        self.assertEqual(server.find_tdf_int(capture, "MODE"), 3)
        self.assertEqual(server.find_tdf_int(capture, "DUR"), 18000)
        self.assertEqual(len(server.find_tdf_field(capture, "ATTR", 0x05)), 56)
        self.assertEqual(len(server.find_tdf_field(capture, "CRIT", 0x03)), 338)
        self.assertEqual(len(server.find_tdf_field(capture, "PNET", 0x06)), 45)
        setup = server.make_game_setup_notification_payload(capture)
        self.assertIn(server.tdf_string_field("VSTR", "Retail-0.9"), setup)
        self.assertIn(server.tdf_int_list("CAP", (12, 0)), setup)
        self.assertIn(server.tdf_int_field("NTOP", 0x82), setup)

    def test_game_setup_contains_required_replicated_data(self):
        request = (server.tdf_string_field("GVER", "nfs-test") +
                   server.tdf_int_field("PMAX", 8) +
                   server.tdf_int_field("GSET", 0x11F) + b"\x00")
        payload = server.make_game_setup_notification_payload(request)
        self.assertIsNotNone(server.find_tdf_field(payload, "GAME", 0x03))
        self.assertIsNotNone(server.find_tdf_field(payload, "PROS", 0x04))
        self.assertIsNotNone(server.find_tdf_field(payload, "QUEU", 0x04))
        self.assertIsNotNone(server.find_tdf_field(payload, "REAS", 0x06))
        self.assertIn(server.tdf_string_field("VSTR", "nfs-test"), payload)
        self.assertIn(server.tdf_int_field("GID", 1), payload)

    def test_matchmaking_status_has_session_and_user(self):
        payload = server.make_matchmaking_async_status(23, 1000001)
        self.assertIsNotNone(server.find_tdf_field(payload, "ASIL", 0x04))
        self.assertEqual(server.find_tdf_int(payload, "MSID"), 23)
        self.assertEqual(server.find_tdf_int(payload, "USID"), 1000001)

    def test_matchmaking_cancellation_has_failed_notification_schema(self):
        payload = server.make_matchmaking_failed_payload(23, 1000001)
        self.assertEqual(server.find_tdf_int(payload, "MAXF"), 0)
        self.assertEqual(server.find_tdf_int(payload, "MSID"), 23)
        self.assertEqual(server.find_tdf_int(payload, "RSLT"), 4)
        self.assertEqual(server.find_tdf_int(payload, "USID"), 1000001)

    def test_proxy_protocol_exposes_original_client(self):
        async def run_test():
            reader = asyncio.StreamReader()
            blaze_header = bytes.fromhex(
                "00 00 00 09 00 02 00 00 00 00 00 01"
            )
            reader.feed_data(
                b"PROXY TCP4 192.168.1.45 192.168.1.110 50123 42127\r\n" +
                blaze_header
            )
            reader.feed_eof()
            return await server.read_proxy_header_and_blaze_header(
                reader, ("127.0.0.1", 40000)
            )

        header, peer = asyncio.run(run_test())
        self.assertEqual(header, bytes.fromhex(
            "00 00 00 09 00 02 00 00 00 00 00 01"
        ))
        self.assertEqual(peer, ("192.168.1.45", 50123))

    def test_registry_assigns_stable_unique_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            registry = server.PlayerRegistry(path)
            alex = registry.get("192.168.1.27")
            friend = registry.get("192.168.1.45")
            self.assertEqual(alex.name, "Player1")
            self.assertNotEqual(friend.persona_id, alex.persona_id)
            reloaded = server.PlayerRegistry(path)
            self.assertEqual(
                reloaded.get("192.168.1.45").persona_id,
                friend.persona_id,
            )
            self.assertEqual(
                [profile.persona_id for profile in reloaded.all_profiles()],
                [alex.persona_id, friend.persona_id],
            )

    def test_association_list_contains_other_registered_players(self):
        alex = server.PlayerProfile(
            1000001, 1000001, "Alex", "192.168.1.27"
        )
        friend = server.PlayerProfile(
            1000002, 1000002, "Player2", "192.168.1.45"
        )
        request = (server.encode_tag("ALST", 0x04) + b"\x03\x01" +
                   b"\x00\x00\x00")
        payload = server.make_association_lists_response(
            request, alex.persona_id, (alex, friend)
        )
        self.assertIn(server.tdf_int_field("BLID", friend.persona_id), payload)
        self.assertIn(server.tdf_string_field("PNAM", friend.name), payload)
        self.assertNotIn(server.tdf_string_field("PNAM", alex.name), payload)
        self.assertIn(server.tdf_int_field("TOCT", 1), payload)

    def test_friend_presence_updates_already_online_client(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
                authenticated=True,
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
                authenticated=True,
            )
            server.ACTIVE_SESSIONS[host.profile.persona_id] = host
            server.ACTIVE_SESSIONS[friend.profile.persona_id] = friend
            await server.announce_friend_presence(friend)
            return bytes(host_writer.data), bytes(friend_writer.data)

        host_data, friend_data = asyncio.run(run_test())
        self.assertIn(b"\x00\x19\x00\x01\x00\x00\x20\x00", host_data)
        self.assertIn(server.tdf_string_field("PNAM", "Player2"), host_data)
        self.assertIn(server.tdf_string_field("NAME", "Alex"), friend_data)

    def test_most_wanted_list_matches_retail_pagerunner_schema(self):
        profile = server.PlayerProfile(
            1000001, 1000001, "Alex", "192.168.1.27"
        )
        model = __import__("json").loads(
            server.make_most_wanted_list_body(profile, 26215)
        )
        self.assertTrue(model["IsCachedData"])
        self.assertEqual(model["LocalUserPosition"], 1)
        self.assertEqual(model["LocalUserPoints"], 26215)
        self.assertEqual(len(model["MostWantedList"]), 1)
        row = model["MostWantedList"][0]
        self.assertEqual(row["id"], 1000001)
        self.assertEqual(row["uid"], 1000001)
        self.assertEqual(row["origin_name"], "Alex")
        self.assertEqual(row["raw_total_bounty"], 26215)
        self.assertTrue(row["isLocal"])
        self.assertIn("rich_presence", row)

    def test_online_progression_is_object_with_required_dlc_vehicles(self):
        async def request(target):
            responses = []

            async def client(reader, writer):
                await server.handle_autolog_http(reader, writer)

            test_server = await asyncio.start_server(client, "127.0.0.1", 0)
            port = test_server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET {target} HTTP/1.1\r\nHost: t\r\n\r\n".encode())
            await writer.drain()
            responses.append(await reader.read())
            writer.close()
            test_server.close()
            await test_server.wait_closed()
            return responses[0].split(b"\r\n\r\n", 1)[1]

        base = ("/?session=S&serviceName=GAMEPC&platform=PC&language=ru"
                "&version=086&flavour=pagerunner&pName=%3F%3F")
        body = json.loads(asyncio.run(request(base)))
        self.assertEqual(body["onlineProgression"], {
            str(vehicle): {"A": vehicle, "E": 2115001}
            for vehicle in (2076005, 2076070, 2076137, 2076203, 2076266)
        })
        self.assertEqual(body["Patches"], {})
        self.assertEqual(body["milestonePatches"], {})
        other = json.loads(asyncio.run(request(base + "&eventId=100&type=Survival")))
        self.assertEqual(other, {"status": "ok"})
        manifest = json.loads(asyncio.run(request("/")))
        self.assertIs(manifest["serverActive"], True)

    def test_extended_data_contains_player_stat_keys(self):
        payload = server.make_user_extended_data_update_notification()
        expected = (server.encode_tag("DMAP", 0x05) + b"\x00\x00" +
                    server.tdf_varint(7))
        # Wii U fork: 0x70004 = Double SP seconds left (real-clock schedule).
        for key, value in ((0x70001, 0x409A), (0x70004, server.double_sp_seconds_left()),
                           (0x70005, 1), (0x70008, 1),
                           (0x70009, 1), (0x7000A, 1), (0x7000B, 1)):
            expected += server.tdf_varint(key) + server.tdf_varint(value)
        self.assertIn(expected, payload)

    def test_extended_data_update_identifies_its_user(self):
        payload = server.make_user_extended_data_update_notification(
            user_id=1000002)
        self.assertTrue(payload.endswith(
            server.tdf_int_field("USID", 1000002) + b"\x00"))
        self.assertTrue(payload.startswith(server.encode_tag("DATA", 0x03)))

    def test_stat_group_response_keeps_requested_name(self):
        request = server.tdf_string_field("NAME", "license_plate")
        response = server.make_stat_group_response(request)
        self.assertEqual(server.find_tdf_string(response, "NAME"), "license_plate")
        self.assertEqual(server.find_tdf_string(response, "CNAM"), "license_plate")

    def test_stats_async_notification_uses_only_safe_scope_keys(self):
        def request(name, view_id):
            return (server.tdf_string_field("NAME", name) +
                    server.tdf_int_field("VID", view_id))

        plate = server.make_stats_async_notification(request("license_plate", 1))
        self.assertEqual(server.find_tdf_string(plate, "GRNM"), "license_plate")
        self.assertEqual(server.find_tdf_string(plate, "KEY"), "No_Scope_Defined")
        self.assertEqual(server.find_tdf_int(plate, "LAST"), 1)
        self.assertEqual(server.find_tdf_int(plate, "VID"), 1)
        self.assertIn(bytes.fromhex("cf 4c c0 03 00"), plate)
        for name in ("online_challenge", "online_concours_challenge",
                     "license_plate_unlock", ""):
            other = server.make_stats_async_notification(request(name, 2))
            self.assertEqual(server.find_tdf_string(other, "GRNM"), name)
            self.assertEqual(server.find_tdf_string(other, "KEY"), "")
            self.assertEqual(server.find_tdf_int(other, "VID"), 2)

    def test_top_speedwall_info_is_keyed_by_decimal_event_id(self):
        model = __import__("json").loads(server.make_top_speedwall_body({
            "event0": ["983491"],
            "event1": ["1107551"],
            "action": ["TopOfSpeedwall"],
        }))
        self.assertEqual(set(model), {"info"})
        self.assertEqual(set(model["info"]), {"983491", "1107551"})
        self.assertEqual(model["info"]["983491"], {
            "No1ScoreToBeat": 0,
            "No1TimeToBeatMS": 0,
        })

    def test_matchmaking_game_keeps_two_unique_players_in_roster(self):
        host_profile = server.PlayerProfile(
            1000001, 1000001, "Alex", "192.168.1.27"
        )
        friend_profile = server.PlayerProfile(
            1000002, 1000002, "Player2", "192.168.1.45"
        )
        host = server.ClientSession(
            object(), ("127.0.0.1", 1), ("192.168.1.27", 5000),
            host_profile,
        )
        friend = server.ClientSession(
            object(), ("127.0.0.1", 2), ("192.168.1.45", 5001),
            friend_profile,
        )
        request = (server.tdf_string_field("GVER", "nfs-test") +
                   server.tdf_int_field("PMAX", 8) +
                   server.tdf_int_field("GSET", 0x11F) + b"\x00")
        game = server.create_game_record(host, request)
        member = server.add_session_to_game(game, friend, request)
        self.assertEqual(member.slot, 1)
        self.assertIs(server.find_matchmaking_game(friend), None)
        setup = server.make_game_setup_notification_payload(
            game.request_payload,
            game_id=game.game_id,
            roster=sorted(game.members.values(), key=lambda item: item.slot),
            recipient_id=friend_profile.persona_id,
        )
        self.assertIn(server.tdf_string_field("NAME", "Alex"), setup)
        self.assertIn(server.tdf_string_field("NAME", "Player2"), setup)
        self.assertEqual(setup.count(server.encode_tag("NAME", 0x01)), 2)

    def test_host_mesh_update_completes_the_target_player(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
                matchmaking_id=33,
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)
            friend_member = server.add_session_to_game(game, friend, b"\x00")
            target = (server.tdf_int_field("FLGS", 0) +
                      server.tdf_int_field("PID", friend.profile.persona_id) +
                      server.tdf_int_field("STAT", 2) + b"\x00")
            request = (server.tdf_int_field("GID", game.game_id) +
                       server.encode_tag("TARG", 0x04) + b"\x03\x01" +
                       target + b"\x00")
            await server.handle_game_manager_request(
                host, 0x001D, 14, request, host.client_peer
            )
            return game, friend_member, friend, host_writer, friend_writer

        game, member, friend, host_writer, friend_writer = asyncio.run(run_test())
        self.assertEqual(member.state, 4)
        self.assertEqual(friend.matchmaking_id, 0)
        expected_pid = server.tdf_int_field("PID", friend.profile.persona_id)
        for writer in (host_writer, friend_writer):
            data = bytes(writer.data)
            self.assertIn(b"\x00\x04\x00\x74\x00\x00\x20\x00", data)
            self.assertIn(b"\x00\x04\x00\x1e\x00\x00\x20\x00", data)
            self.assertIn(expected_pid, data)

    def test_game_mutations_update_replicated_state_and_notify_members(self):
        async def run_test():
            writer = FakeWriter()
            host = server.ClientSession(
                writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)

            settings = (server.tdf_int_field("GID", game.game_id) +
                        server.tdf_int_field("GSET", 0x51F) + b"\x00")
            await server.handle_game_manager_request(
                host, 0x0004, 20, settings, host.client_peer
            )

            capacity = (server.tdf_int_field("GID", game.game_id) +
                        server.tdf_int_list("PCAP", (12, 0)) +
                        server.tdf_int_field("TCAP", 0) + b"\x00")
            await server.handle_game_manager_request(
                host, 0x0005, 21, capacity, host.client_peer
            )

            attrs = (server.encode_tag("ATTR", 0x05) + b"\x01\x01\x01" +
                     server.tdf_string("MODE") + server.tdf_string("PUBLIC"))
            await server.handle_game_manager_request(
                host, 0x0007, 22,
                attrs + server.tdf_int_field("GID", game.game_id) + b"\x00",
                host.client_peer,
            )

            player_attrs = (server.encode_tag("ATTR", 0x05) + b"\x01\x01\x01" +
                            server.tdf_string("CAR") +
                            server.tdf_string("AUDI_RS3"))
            await server.handle_game_manager_request(
                host, 0x0008, 23,
                player_attrs + server.tdf_int_field("GID", game.game_id) +
                server.tdf_int_field("PID", host.profile.persona_id) + b"\x00",
                host.client_peer,
            )
            return game, writer

        game, writer = asyncio.run(run_test())
        self.assertEqual(game.settings, 0x51F)
        self.assertEqual(game.capacity, 12)
        self.assertIn(server.tdf_string("MODE"), game.attributes)
        self.assertEqual(game.members[game.host_id].player_attrs[:4],
                         server.encode_tag("PATT", 0x05))
        data = bytes(writer.data)
        for notification in (0x006E, 0x006F, 0x0050, 0x005A):
            self.assertIn(
                b"\x00\x04" + notification.to_bytes(2, "big") +
                b"\x00\x00\x20\x00",
                data,
            )
        browser = server.make_game_browser_data(game)
        self.assertIn(server.tdf_int_list("CAP", (12, 0)), browser)
        self.assertIn(server.tdf_int_field("GSET", 0x51F), browser)
        self.assertIn(server.tdf_string("MODE"), browser)

    def test_two_matchmaking_clients_share_one_game_and_receive_notifications(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
            )
            request = (server.tdf_string_field("GVER", "nfs-test") +
                       server.tdf_int_field("PMAX", 8) +
                       server.tdf_int_field("GSET", 0x11F) + b"\x00")
            await server.handle_game_manager_request(
                host, 0x000D, 1, request, host.client_peer
            )
            host_before_join = len(host_writer.data)
            self.assertIn(b"\x00\x04\x00\x0c\x00\x00\x20\x00",
                          bytes(host_writer.data))
            self.assertNotIn(b"\x00\x04\x00\x14\x00\x00\x20\x00",
                             bytes(host_writer.data))
            await server.handle_game_manager_request(
                friend, 0x000D, 2, request, friend.client_peer
            )
            return host, friend, host_writer, friend_writer, host_before_join

        host, friend, host_writer, friend_writer, host_before = asyncio.run(
            run_test()
        )
        self.assertEqual(host.game_id, friend.game_id)
        self.assertEqual(len(server.ACTIVE_GAMES[host.game_id].members), 2)
        self.assertTrue(server.ACTIVE_GAMES[host.game_id].setup_announced)
        self.assertGreater(len(host_writer.data), host_before)
        self.assertIn(b"\x00\x04\x00\x14\x00\x00\x20\x00",
                      bytes(host_writer.data[host_before:]))
        self.assertIn(b"\x00\x04\x00\x14\x00\x00\x20\x00",
                      bytes(friend_writer.data))
        self.assertIn(b"\x00\x04\x00\x18\x00\x00\x20\x00",
                      bytes(friend_writer.data))
        self.assertNotIn(b"\x00\x04\x00\x18\x00\x00\x20\x00",
                         bytes(host_writer.data))
        self.assertNotIn(b"\x00\x04\x00\x16\x00\x00\x20\x00",
                         bytes(friend_writer.data))
        self.assertIn(server.tdf_string_field("NAME", "Alex"),
                      bytes(friend_writer.data))
        self.assertIn(server.tdf_string_field("NAME", "Player2"),
                      bytes(friend_writer.data))

    def test_non_host_leave_notifies_both_players_and_browser_subscribers(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            browser_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
                authenticated=True,
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
                authenticated=True,
            )
            browser = server.ClientSession(
                browser_writer, ("127.0.0.1", 3), ("192.168.1.46", 5002),
                server.PlayerProfile(1000003, 1000003, "Player3", "192.168.1.46"),
                authenticated=True, game_list_id=9, game_list_subscription=True,
            )
            server.ACTIVE_SESSIONS.update({
                host.profile.persona_id: host,
                friend.profile.persona_id: friend,
                browser.profile.persona_id: browser,
            })
            request = (server.tdf_string_field("GNAM", "Leave Test") +
                       server.tdf_int_field("PMAX", 8) + b"\x00")
            game = server.create_game_record(host, request, connected_host=True)
            server.add_session_to_game(game, friend, request)
            await server.detach_session_from_game(friend, reason=6)
            return game, host_writer, friend_writer, browser_writer

        game, host_writer, friend_writer, browser_writer = asyncio.run(run_test())
        self.assertEqual(set(game.members), {1000001})
        self.assertEqual(game.game_id, server.ACTIVE_GAMES[game.game_id].game_id)
        for writer in (host_writer, friend_writer):
            self.assertIn(b"\x00\x04\x00\x28\x00\x00\x20\x00", bytes(writer.data))
            self.assertIn(server.tdf_int_field("PID", 1000002), bytes(writer.data))
        browser_data = bytes(browser_writer.data)
        self.assertIn(b"\x00\x04\x00\xc9\x00\x00\x20\x00", browser_data)
        self.assertIn(server.tdf_int_field("GLID", 9), browser_data)
        self.assertIn(server.tdf_int_list("PCNT", (1, 0)), browser_data)

    def test_cancel_single_player_matchmaking_without_game_setup(self):
        async def run_test():
            writer = FakeWriter()
            player = server.ClientSession(
                writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            await server.handle_game_manager_request(
                player, 0x000D, 1,
                server.tdf_int_field("PMAX", 8) + b"\x00",
                player.client_peer,
            )
            game_id = player.game_id
            self.assertNotIn(b"\x00\x04\x00\x14\x00\x00\x20\x00",
                             bytes(writer.data))
            writer.data.clear()
            await server.handle_game_manager_request(
                player, 0x000E, 2,
                server.tdf_int_field("MSID", player.matchmaking_id) + b"\x00",
                player.client_peer,
            )
            return game_id, player, bytes(writer.data)

        game_id, player, data = asyncio.run(run_test())
        reply = server.make_blaze_response(0x0004, 0x000E, 2)
        notify = server.make_blaze_notification(
            0x0004, 0x000A,
            server.make_matchmaking_failed_payload(1, 1000001),
        )
        self.assertEqual(data, reply + notify)
        self.assertEqual(player.game_id, 0)
        self.assertNotIn(game_id, server.ACTIVE_GAMES)

    def test_host_disconnect_removes_game_from_browser(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            browser_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
                authenticated=True,
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
                authenticated=True,
            )
            browser = server.ClientSession(
                browser_writer, ("127.0.0.1", 3), ("192.168.1.46", 5002),
                server.PlayerProfile(1000003, 1000003, "Player3", "192.168.1.46"),
                authenticated=True, game_list_id=11, game_list_subscription=True,
            )
            server.ACTIVE_SESSIONS.update({
                host.profile.persona_id: host,
                friend.profile.persona_id: friend,
                browser.profile.persona_id: browser,
            })
            game = server.create_game_record(
                host, server.tdf_int_field("PMAX", 8) +
                server.tdf_int_field("GSET", 0x10F) + b"\x00",
                connected_host=True,
            )
            server.add_session_to_game(game, friend, b"\x00")
            await server.detach_session_from_game(host, reason=2)
            return game.game_id, friend, friend_writer, browser_writer

        game_id, friend, friend_writer, browser_writer = asyncio.run(run_test())
        self.assertNotIn(game_id, server.ACTIVE_GAMES)
        self.assertEqual(friend.game_id, 0)
        self.assertIn(b"\x00\x04\x00\x10\x00\x00\x20\x00",
                      bytes(friend_writer.data))
        browser_data = bytes(browser_writer.data)
        self.assertIn(server.tdf_int_list("REMV", (game_id,)), browser_data)
        self.assertIn(server.tdf_int_field("GLID", 11), browser_data)

    def test_migratable_host_disconnect_promotes_remaining_player(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            browser_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
                authenticated=True,
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
                authenticated=True,
            )
            browser = server.ClientSession(
                browser_writer, ("127.0.0.1", 3), ("192.168.1.46", 5002),
                server.PlayerProfile(1000003, 1000003, "Player3", "192.168.1.46"),
                authenticated=True, game_list_id=12, game_list_subscription=True,
            )
            server.ACTIVE_SESSIONS.update({
                host.profile.persona_id: host,
                friend.profile.persona_id: friend,
                browser.profile.persona_id: browser,
            })
            request = (server.tdf_int_field("PMAX", 8) +
                       server.tdf_int_field("GSET", 0x11F) + b"\x00")
            game = server.create_game_record(host, request, connected_host=True)
            server.add_session_to_game(game, friend, b"\x00")
            await server.detach_session_from_game(host, reason=2)
            return game, friend, friend_writer, browser_writer

        game, friend, friend_writer, browser_writer = asyncio.run(run_test())
        self.assertIn(game.game_id, server.ACTIVE_GAMES)
        self.assertEqual(game.host_id, friend.profile.persona_id)
        self.assertEqual(friend.game_id, game.game_id)
        friend_data = bytes(friend_writer.data)
        self.assertIn(b"\x00\x04\x00\x46\x00\x00\x20\x00", friend_data)
        self.assertIn(b"\x00\x04\x00\x3c\x00\x00\x20\x00", friend_data)
        self.assertIn(server.tdf_int_field("HOST", friend.profile.persona_id),
                      friend_data)
        browser_data = bytes(browser_writer.data)
        self.assertIn(server.tdf_int_field("HOST", friend.profile.persona_id),
                      browser_data)
        setup = server.make_list_games_response((game,))
        self.assertIn(server.tdf_int_field("HSLT", 1), setup)

    def test_destroy_game_notifies_every_member_after_rpc_response(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)
            server.add_session_to_game(game, friend, b"\x00")
            request = server.tdf_int_field("GID", game.game_id) + b"\x00"
            await server.handle_game_manager_request(
                host, 0x0002, 77, request, host.client_peer
            )
            return game.game_id, host, friend, host_writer, friend_writer

        game_id, host, friend, host_writer, friend_writer = asyncio.run(run_test())
        self.assertNotIn(game_id, server.ACTIVE_GAMES)
        self.assertEqual(host.game_id, 0)
        self.assertEqual(friend.game_id, 0)
        host_data = bytes(host_writer.data)
        response_at = host_data.find(b"\x00\x04\x00\x02\x00\x00\x10\x00\x00\x4d")
        notify_at = host_data.find(b"\x00\x04\x00\x10\x00\x00\x20\x00")
        self.assertGreaterEqual(response_at, 0)
        self.assertGreater(notify_at, response_at)
        self.assertIn(server.tdf_int_field("REAS", 0), host_data[notify_at:])
        self.assertIn(b"\x00\x04\x00\x10\x00\x00\x20\x00",
                      bytes(friend_writer.data))

    def test_custom_data_and_game_session_update_are_replicated(self):
        async def run_test():
            writer = FakeWriter()
            host = server.ClientSession(
                writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)
            custom = b"\x00car\xffsetup"
            await server.handle_game_manager_request(
                host, 0x0012, 31,
                server.tdf_blob_field("CDAT", custom) +
                server.tdf_int_field("GID", game.game_id) +
                server.tdf_int_field("PID", host.profile.persona_id) + b"\x00",
                host.client_peer,
            )
            nonce = b"nonce\x00bytes"
            session_blob = b"session\xffbytes"
            await server.handle_game_manager_request(
                host, 0x001A, 32,
                server.tdf_int_field("GID", game.game_id) +
                server.tdf_blob_field("XNNC", nonce) +
                server.tdf_blob_field("XSES", session_blob) + b"\x00",
                host.client_peer,
            )
            return game, writer, custom, nonce, session_blob

        game, writer, custom, nonce, session_blob = asyncio.run(run_test())
        member = game.members[game.host_id]
        self.assertEqual(member.custom_data, custom)
        self.assertEqual(game.xnet_nonce, nonce)
        self.assertEqual(game.xnet_session, session_blob)
        data = bytes(writer.data)
        self.assertIn(b"\x00\x04\x00\x5f\x00\x00\x20\x00", data)
        self.assertIn(b"\x00\x04\x00\x73\x00\x00\x20\x00", data)
        setup = server.make_list_games_response((game,))
        self.assertIn(server.tdf_blob_field("BLOB", custom), setup)
        self.assertIn(server.tdf_blob_field("XNNC", nonce), setup)
        self.assertIn(server.tdf_blob_field("XSES", session_blob), setup)

    def test_replay_name_admin_and_team_changes_persist_and_notify(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)
            server.add_session_to_game(game, friend, b"\x00")
            requests = (
                (0x0013, server.tdf_int_field("GID", game.game_id) + b"\x00"),
                (0x0027, server.tdf_int_field("GID", game.game_id) +
                 server.tdf_string_field("GNAM", "Friends Lobby") + b"\x00"),
                (0x006A, server.tdf_int_field("GID", game.game_id) +
                 server.tdf_int_field("PID", friend.profile.persona_id) + b"\x00"),
                (0x006C, server.tdf_int_field("GID", game.game_id) +
                 server.tdf_int_field("PID", friend.profile.persona_id) +
                 server.tdf_int_field("TIDX", 2) + b"\x00"),
                (0x006D, server.tdf_int_field("GID", game.game_id) +
                 server.tdf_int_field("NTID", 77) +
                 server.tdf_int_field("TIDX", 2) + b"\x00"),
            )
            for msg_id, (command, request) in enumerate(requests, 40):
                await server.handle_game_manager_request(
                    host, command, msg_id, request, host.client_peer
                )
            return game, host_writer, friend_writer, friend

        game, host_writer, friend_writer, friend = asyncio.run(run_test())
        self.assertEqual(game.state, 130)
        self.assertEqual(game.name, "Friends Lobby")
        self.assertIn(friend.profile.persona_id, game.admins)
        self.assertEqual(game.members[friend.profile.persona_id].team_index, 2)
        self.assertEqual(game.team_ids[2], 77)
        for writer in (host_writer, friend_writer):
            data = bytes(writer.data)
            for notification in (0x0064, 0x00E6, 0x00CA, 0x0075, 0x0076):
                self.assertIn(
                    b"\x00\x04" + notification.to_bytes(2, "big") +
                    b"\x00\x00\x20\x00",
                    data,
                )
        setup = server.make_list_games_response((game,))
        self.assertIn(server.tdf_string_field("GNAM", "Friends Lobby"), setup)
        self.assertIn(server.tdf_int_list(
            "ADMN", (1000001, 1000002)
        ), setup)
        self.assertIn(server.tdf_int_field("TIDX", 2), setup)

    def test_ban_removes_player_and_rejects_rejoin(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)
            server.add_session_to_game(game, friend, b"\x00")
            await server.handle_game_manager_request(
                host, 0x001B, 60,
                server.tdf_int_field("GID", game.game_id) +
                server.tdf_int_list("PLST", (friend.profile.persona_id,)) + b"\x00",
                host.client_peer,
            )
            friend_writer.data.clear()
            await server.handle_game_manager_request(
                friend, 0x0009, 61,
                server.tdf_int_field("GID", game.game_id) + b"\x00",
                friend.client_peer,
            )
            host_writer.data.clear()
            await server.handle_game_manager_request(
                host, 0x0021, 62,
                server.tdf_int_field("GID", game.game_id) + b"\x00",
                host.client_peer,
            )
            return game, host_writer, friend_writer, friend

        game, host_writer, friend_writer, friend = asyncio.run(run_test())
        self.assertNotIn(friend.profile.persona_id, game.members)
        self.assertIn(friend.profile.persona_id, game.banned_players)
        # Error code 110 is the fourth 16-bit member in a Blaze response.
        self.assertEqual(bytes(friend_writer.data)[6:8], b"\x00\x6e")
        self.assertIn(
            server.tdf_int_list("BANM", (friend.profile.persona_id,)),
            bytes(host_writer.data),
        )

    def test_explicit_host_migration_changes_setup_host(self):
        async def run_test():
            host_writer = FakeWriter()
            friend_writer = FakeWriter()
            host = server.ClientSession(
                host_writer, ("127.0.0.1", 1), ("192.168.1.27", 5000),
                server.PlayerProfile(1000001, 1000001, "Alex", "192.168.1.27"),
            )
            friend = server.ClientSession(
                friend_writer, ("127.0.0.1", 2), ("192.168.1.45", 5001),
                server.PlayerProfile(1000002, 1000002, "Player2", "192.168.1.45"),
            )
            game = server.create_game_record(host, b"\x00", connected_host=True)
            server.add_session_to_game(game, friend, b"\x00")
            await server.handle_game_manager_request(
                host, 0x0017, 70,
                server.tdf_int_field("GID", game.game_id) +
                server.tdf_int_field("HOST", friend.profile.persona_id) + b"\x00",
                host.client_peer,
            )
            return game, host_writer, friend_writer

        game, host_writer, friend_writer = asyncio.run(run_test())
        self.assertEqual(game.host_id, 1000002)
        self.assertEqual(game.admins, {1000002})
        for writer in (host_writer, friend_writer):
            data = bytes(writer.data)
            self.assertIn(b"\x00\x04\x00\x46\x00\x00\x20\x00", data)
            self.assertIn(b"\x00\x04\x00\x3c\x00\x00\x20\x00", data)
        setup = server.make_list_games_response((game,))
        self.assertIn(server.tdf_int_field("HPID", 1000002), setup)
        self.assertIn(server.tdf_int_field("HSLT", 1), setup)

    def test_extended_data_field_types_match_client_schema(self):
        # Member table at 0x0105DAD0: a wrong wire type fails the whole decode.
        payload = server.make_user_extended_data_update_notification()
        self.assertIn(server.encode_tag("CVAR", 0x07) + b"\x00", payload)
        self.assertNotIn(server.encode_tag("CVAR", 0x04), payload)
        for tag, wire_type in (("ADDR", 6), ("BPS", 1), ("CTY", 1),
                               ("DMAP", 5), ("PSLM", 4), ("QDAT", 3),
                               ("ULST", 4)):
            self.assertIn(server.encode_tag(tag, wire_type), payload)

    def test_user_authenticated_identifies_local_user(self):
        # 0x00C04A20 reads UserSessionLoginInfo, not BID/LOTP.
        payload = server.make_user_authenticated_notification(
            1000002, 1000003, "Bob", "key")
        self.assertEqual(server.find_tdf_int(payload, "BUID"), 1000002)
        self.assertEqual(server.find_tdf_int(payload, "PID"), 1000002)
        self.assertEqual(server.find_tdf_int(payload, "UID"), 1000003)
        self.assertIn(server.tdf_string_field("DSNM", "Bob"), payload)
        self.assertIn(server.tdf_string_field("KEY", "key"), payload)
        self.assertNotIn(server.encode_tag("LOTP", 0x00), payload)

    def test_filter_user_text_passes_captured_request(self):
        # Sent by the client while setting the licence plate text.
        request = bytes.fromhex(
            "d2 cc f4 04 03 01 92 9c b4 00 02 d7 4e 34 01 05 73 6e 6f 77 00 00")
        response = server.make_filter_user_text_response(request)
        self.assertEqual(response, bytes.fromhex(
            "d2 cc f4 04 03 01 92 9c b4 00 00 d7 4e 34 01 05 73 6e 6f 77 00 00 00"))

    NETWORK_INFO = bytes.fromhex(
        "86 49 32 06 02 da 1b 35 03 97 8a 70 03 a7 00 00 00 9b 84 c0 8a 18 c2 fc"
        " b4 00 8b 39 00 a6 ea 70 03 a7 00 00 00 9b 84 c0 8a 18 c2 fc b4 00 8b 39"
        " 00 00 ba cb 70 05 01 00 01 06 6c 6f 63 61 6c 00 01 bb 1b f3 03 92 2c 33"
        " 00 00 ba 1d 34 00 01 d6 2c 33 00 9b 84 c0 8a 18 00")

    def test_network_info_keeps_only_address_and_closed_qos(self):
        addr, qdat = server.parse_network_info(self.NETWORK_INFO)
        self.assertEqual(addr[:4], server.encode_tag("ADDR", 0x06))
        self.assertNotIn(server.encode_tag("NLMP", 0x05), addr)
        self.assertEqual(server.tdf_field_end(qdat, 0), len(qdat))

    def test_game_setup_fields_stay_at_their_level(self):
        addr, qdat = server.parse_network_info(self.NETWORK_INFO)
        payload = server.make_game_setup_notification_payload(
            game_id=7, raw_address=addr, raw_qdat=qdat, player_id=1000002,
            player_name="Player2")
        self.assertEqual(server.tdf_field_end(payload, 0) > 0, True)
        game = server.find_tdf_field(payload, "GAME", 0x03)[4:]
        host = server.find_tdf_field(game, "THST", 0x03)[4:]
        self.assertEqual(server.find_tdf_int(host, "HPID"), 1000002)
        self.assertIsNotNone(server.find_tdf_field(payload, "PROS", 0x04))
        self.assertIsNotNone(server.find_tdf_field(payload, "REAS", 0x06))

    def test_extended_update_with_captured_network_info_reaches_usid(self):
        addr, qdat = server.parse_network_info(self.NETWORK_INFO)
        payload = server.make_user_extended_data_update_notification(
            addr, qdat, user_id=1000002)
        qdat_at = payload.index(qdat)
        self.assertEqual(payload[qdat_at + len(qdat):qdat_at + len(qdat) + 3],
                         server.encode_tag("UATT", 0x00)[:3])
        self.assertTrue(payload.endswith(
            server.tdf_int_field("USID", 1000002) + b"\x00"))

    def test_game_setup_uses_full_mesh_for_player_hosted_games(self):
        request = server.tdf_int_field("NTOP", 0x84) + b"\x00"
        payload = server.make_game_setup_notification_payload(request_payload=request)
        game = server.find_tdf_field(payload, "GAME", 0x03)[4:]
        self.assertEqual(server.find_tdf_int(game, "NTOP"), 0x82)

    def test_registry_rename_validates_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            registry = server.PlayerRegistry(path)
            registry.get("192.168.1.27")
            self.assertEqual(registry.rename("192.168.1.27", "Alex"), "")
            friend = registry.get("10.0.0.16")
            self.assertTrue(registry.rename("10.0.0.99", "Ghost"))
            self.assertTrue(registry.rename("10.0.0.16", "bad name"))
            self.assertTrue(registry.rename("10.0.0.16", ""))
            self.assertTrue(registry.rename("10.0.0.16", "x" * 16))
            self.assertTrue(registry.rename("10.0.0.16", "\u0421\u0430\u043d\u044f"))
            self.assertTrue(registry.rename("10.0.0.16", "alex"))
            self.assertEqual(registry.rename("10.0.0.16", "Sanya"), "")
            self.assertEqual(registry.rename("10.0.0.16", "sanya"), "")
            reloaded = server.PlayerRegistry(path).get("10.0.0.16")
            self.assertEqual(reloaded.name, "sanya")
            self.assertEqual(reloaded.persona_id, friend.persona_id)

    def test_rename_endpoint_only_changes_the_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = server.PlayerRegistry(Path(directory) / "players.json")
            registry.get("10.0.0.16")
            registry.get("10.0.0.14")
            original = server.PLAYER_REGISTRY
            server.PLAYER_REGISTRY = registry
            try:
                body = json.loads(server.make_rename_body(
                    "10.0.0.16", {"name": ["Sanya"], "ip": ["10.0.0.14"]}))
                failed = json.loads(server.make_rename_body("10.0.0.14", {}))
            finally:
                server.PLAYER_REGISTRY = original
            self.assertEqual(body["status"], "ok")
            self.assertEqual(failed["status"], "error")
            self.assertEqual(registry.get("10.0.0.16").name, "Sanya")
            self.assertEqual(registry.get("10.0.0.14").name, "Player2")

    PLATE_REPORT = bytes.fromhex(
        "9a ec e8 00 00 c3 2d b4 07 00 cb 0c b4 03 9e 1b 65 07 01 8a fb f3 9a 0f"
        " 9e 1b 65 03 c2 c8 74 05 00 03 01 81 89 7a a6 40 00 00 a0 bb a4 01 d2 19"
        " c0 01 05 73 6e 6f 77 00 00 00 00 9f 2a 64 00 00 9f 4e 70 01 0e 6c 69 63"
        " 65 6e 73 65 5f 70 6c 61 74 65 00 00")

    def test_license_plate_report_is_parsed(self):
        plate = server.parse_license_plate_report(self.PLATE_REPORT)
        self.assertEqual(plate[1], "snow")
        self.assertGreater(plate[0], 0)
        other = self.PLATE_REPORT.replace(b"license_plate", b"license_platf")
        self.assertIsNone(server.parse_license_plate_report(other))

    def test_license_plate_store_persists_per_player(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plates.json"
            store = server.LicensePlateStore(path)
            store.set(1000003, 42, "snow")
            reloaded = server.LicensePlateStore(path)
            self.assertEqual(reloaded.get(1000003), (42, "snow"))
            self.assertIsNone(reloaded.get(1000001))

    def test_stats_view_returns_stored_plate_in_client_order(self):
        request = server.tdf_string_field("NAME", "license_plate") + b"\x00"
        with tempfile.TemporaryDirectory() as directory:
            original = server.LICENSE_PLATES
            server.LICENSE_PLATES = server.LicensePlateStore(Path(directory) / "p.json")
            try:
                empty = server.make_stats_async_notification(request, 1000003)
                server.LICENSE_PLATES.set(1000003, 42, "snow")
                filled = server.make_stats_async_notification(request, 1000003)
                other = server.make_stats_async_notification(request, 1000001)
            finally:
                server.LICENSE_PLATES = original
        self.assertEqual(empty, other)
        self.assertIn(server.encode_tag("STS", 0x03) + b"\x00", empty)
        self.assertIn(server.encode_tag("STAT", 0x04) + b"\x01" + server.tdf_varint(2) +
                      server.tdf_string("42") + server.tdf_string("snow"), filled)
        status = server.find_tdf_field(filled, "STS", 0x03)
        self.assertIsNotNone(status)
        self.assertEqual(server.find_tdf_int(filled, "LAST"), 1)

    def test_playlist_bodies_match_client_parser(self):
        next_list = json.loads(server.make_playlist_body("GetNextPlaylist"))["Playlist"]
        self.assertLessEqual(len(next_list["events"]), 5)
        self.assertTrue(all(isinstance(item["event_id"], int)
                            for item in next_list["events"]))
        self.assertTrue(all(item["restriction_id"] == 0
                            for item in next_list["events"]))
        self.assertIsInstance(next_list["name"], str)
        vote = json.loads(server.make_playlist_body("GetVotePlaylists"))
        self.assertEqual(set(vote), {"VoteA", "VoteB"})

    def test_speedlist_events_are_random_and_skip_previous(self):
        previous = set(server.SPEEDLIST_EVENT_POOL[:10])
        picks = [tuple(server.pick_speedlist_events(previous)) for _ in range(20)]
        for events in picks:
            self.assertEqual(len(set(events)), server.SPEEDLIST_LENGTH)
            self.assertTrue(set(events) <= set(server.SPEEDLIST_EVENT_POOL))
            self.assertFalse(set(events) & previous)
        self.assertGreater(len(set(picks)), 1)


if __name__ == "__main__":
    unittest.main()
