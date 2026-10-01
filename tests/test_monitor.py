import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


_config_dir = tempfile.TemporaryDirectory()
os.environ["ROBLOX_REJOINER_CONFIG"] = os.path.join(_config_dir.name, "config.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_manager  # noqa: E402
import roblox_monitor  # noqa: E402

JOIN = "2026-09-30T07:17:12.013Z,1.0,038c,6 [FLog::Output] ! Joining game '8a90b63e-8476-4aa5-b383-8b32de4866df' place 77649408247578 at 10.0.0.1\n"
LOADTIME = ("2026-09-30T07:17:12.013Z,1.0,038c,6 [FLog::GameJoinLoadTime] Report game_join_loadtime: placeid:77649408247578, "
            "join_time:0.48, universeid:9931749389, referral_page:RequestGame, sid:x, clienttime:1790752632.6, userid:10822113636, \n")
TELEPORT = "2026-09-30T07:20:00.000Z,9.0,65f4,7 [FLog::Network] Disconnection Notification. Reason: 285\n"
KICK = "2026-09-30T07:12:55.813Z,811.8,65f4,7 [FLog::Network] Disconnection Notification. Reason: 267\n"
# a join that timed out (30 Sep 8:26 PM): the lines Roblox writes before it shows the error 279 box
CONNECT_TIMEOUT = ("2026-10-01T00:26:33.816Z,10542.816406,3ee4,6,Error [DFLog::RbxTransportDummyClient] Failed to establish connection "
                   "to server at 128.116.48.33:57164, reason None: 0 (Connection attempt timed out)\n")
CONNECT_FAILED = ("2026-10-01T00:26:43.982Z,10552.982422,43a4,6,Error [DFLog::NetworkClient] Failed to connect to server at "
                  "128.116.48.33|56285, no response.\n")
# Roblox 0.741 writes no "Disconnection Notification": the lines of a kick by hand (1 Oct 2:38 AM) and by the anti-cheat
# (30 Sep 10:40 PM), and of a teleport
KICK_0741 = ("2026-10-01T07:38:54.940Z,15859.940430,3738,7 [FLog::Network] Players service requests player disconnect\n"
             "2026-10-01T07:38:54.945Z,15859.945312,3738,7,Warning [FLog::Network] Lost connection with reason : You have been "
             "kicked from the game\n")
SERVER_KICK_0741 = ("2026-10-01T03:40:55.588Z,1580.588989,1c74,6,Error [FLog::CreatorError] Error: Server Kick Message: Kicked by "
                    "anti-cheat.; Metadata: \n"
                    "2026-10-01T03:40:56.621Z,1581.621216,1c74,7 [FLog::Network] Client has been disconnected with reason: You have "
                    "been kicked from the game\n"
                    "2026-10-01T03:40:56.628Z,1581.628296,3738,7,Warning [FLog::Network] Lost connection with reason : You have been "
                    "kicked from the game\n")
TELEPORT_0741 = ("2026-10-01T05:49:52.382Z,9317.382812,3738,6,Info [DFLog::RbxTransportDummyClient] Disconnected from server for "
                 "reason: Player: 285 (DisconnectClientInitiated)\n")


class ClientLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "0.740_20260930T071711Z_Player_B3C54_last.log"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, text):
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)

    def test_account_and_kick_are_read(self):
        self.write(JOIN + LOADTIME)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertEqual(client.poll(), [("joined", None)])
        self.assertEqual(client.user_id, 10822113636)
        self.assertEqual(client.place_id, 77649408247578)
        self.write(KICK)
        self.assertEqual(client.poll(), [("kicked", 267)])
        self.assertEqual(client.poll(), [])

    def test_teleport_is_not_a_kick(self):
        self.write(JOIN + LOADTIME + TELEPORT + JOIN)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertNotIn("kicked", [kind for kind, _ in client.poll()])
        self.assertIsNone(client.kicked_reason)

    def test_kick_then_rejoin_in_the_same_window_clears_it(self):
        self.write(JOIN + LOADTIME + KICK + JOIN)
        client = roblox_monitor.Client(1234, str(self.path))
        client.poll()
        self.assertIsNone(client.kicked_reason)

    def test_failed_join_is_a_kick(self):
        # the "Connection Failed ... No response from server (Error Code: 279)" box writes no Disconnection Notification
        self.write(JOIN + LOADTIME)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertEqual(client.poll(), [("joined", None)])
        self.write(JOIN + CONNECT_TIMEOUT + CONNECT_FAILED)
        self.assertEqual(client.poll(), [("joined", None), ("kicked", 279)])
        self.assertEqual(client.kicked_reason, 279)
        self.assertEqual(client.read_message(), roblox_monitor.CONNECT_FAILED_TEXT)
        self.assertEqual(client.poll(), [])

    def test_failed_join_then_good_join_is_not_a_kick(self):
        # Retry pressed by hand (or the whole log read at start-up): a later join means the window is fine
        self.write(JOIN + LOADTIME + JOIN + CONNECT_FAILED + JOIN)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertNotIn("kicked", [kind for kind, _ in client.poll()])
        self.assertIsNone(client.kicked_reason)

    def test_window_already_stuck_when_the_monitor_starts(self):
        self.write(JOIN + LOADTIME + TELEPORT + JOIN + CONNECT_TIMEOUT + CONNECT_FAILED)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertIn(("kicked", 279), client.poll())
        self.assertEqual(client.user_id, 10822113636)

    def test_incomplete_line_is_completed_once(self):
        self.write(JOIN + LOADTIME + "2026-09-30T07:12:55.813Z,811.8,65f4,7 [FLog::Network] Disconnection Notif")
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertEqual(client.poll(), [("joined", None)])
        self.write("ication. Reason: 267\n")
        self.assertEqual(client.poll(), [("kicked", 267)])

    def test_disconnected_box_on_roblox_0741_is_a_kick(self):
        self.write(JOIN + LOADTIME)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertEqual(client.poll(), [("joined", None)])
        self.write(KICK_0741)
        self.assertEqual(client.poll(), [("kicked", 267)])
        self.assertEqual(client.read_message(), "You have been kicked from the game")
        self.assertEqual(client.poll(), [])

    def test_server_kick_on_roblox_0741_is_one_kick_with_the_games_text(self):
        self.write(JOIN + LOADTIME + SERVER_KICK_0741)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertEqual(client.poll(), [("joined", None), ("kicked", 267)])
        self.assertEqual(client.read_message(), "Kicked by anti-cheat.")

    def test_teleport_on_roblox_0741_is_not_a_kick(self):
        self.write(JOIN + LOADTIME + TELEPORT_0741 + JOIN)
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertNotIn("kicked", [kind for kind, _ in client.poll()])
        self.assertIsNone(client.kicked_reason)

    def test_unknown_disconnect_text_still_counts(self):
        self.write(JOIN + LOADTIME + "2026-10-01T08:00:00.000Z,1.0,3738,7,Warning [FLog::Network] Lost connection with reason : "
                   "The server has shut down\n")
        client = roblox_monitor.Client(1234, str(self.path))
        self.assertIn(("kicked", 0), client.poll())
        self.assertEqual(roblox_monitor.reason_text(0), "disconnected")


class LocatorTests(unittest.TestCase):
    def test_exploitstrap_log_maps_pid_to_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs = Path(temp_dir) / "Logs"
            logs.mkdir()
            (logs / "ExploitStrap_1.log").write_text(
                "2026-09-30T07:17:09Z [Bootstrapper::StartRoblox] Started Roblox (PID 8344), waiting for log file\n"
                "2026-09-30T07:17:11Z [Bootstrapper::StartRoblox] Got log file as C:\\logs\\0.740_Player_B3C54_last.log\n",
                encoding="utf-8",
            )
            with mock.patch.object(roblox_monitor, "EXPLOITSTRAP_DIR", temp_dir):
                locator = roblox_monitor.ClientLocator()
                locator.scan_exploitstrap()
            self.assertEqual(locator.pid_to_log[8344], "C:\\logs\\0.740_Player_B3C54_last.log")


def wait_for_workers(timeout=10):
    deadline = time.monotonic() + timeout
    while any(worker.is_alive() for worker in list(roblox_monitor._workers.values())) and time.monotonic() < deadline:
        time.sleep(0.05)


class RejoinTests(unittest.TestCase):
    def setUp(self):
        roblox_monitor.MONITOR_ACTIVE = True
        with roblox_monitor.state_lock:
            roblox_monitor.state["monitoring"] = True
            roblox_monitor.state["accounts"].clear()
            roblox_monitor.state["events"].clear()
        roblox_monitor._names[111] = "FarmAlt"
        roblox_monitor.clients.clear()

    def tearDown(self):
        roblox_monitor.clients.clear()
        roblox_monitor._workers.clear()

    def test_kick_closes_only_that_client_and_reopens_that_account(self):
        kicked = roblox_monitor.Client(1, os.devnull)
        kicked.user_id, kicked.joined_at = 111, time.monotonic()
        other = roblox_monitor.Client(2, os.devnull)
        other.user_id, other.joined_at = 222, time.monotonic()
        roblox_monitor.clients.update({1: kicked, 2: other})

        def fake_launch(name):
            fresh = roblox_monitor.Client(3, os.devnull)
            fresh.user_id, fresh.joined_at = 111, time.monotonic() + 1
            roblox_monitor.clients[3] = fresh
            return True, "OK"

        with mock.patch.object(roblox_monitor, "close_client") as close, \
                mock.patch.object(roblox_monitor, "launch_account", side_effect=fake_launch) as launch, \
                mock.patch.object(roblox_monitor, "REJOIN_DELAY", 0), \
                mock.patch.object(roblox_monitor, "wait_for_internet", return_value=True):
            roblox_monitor._on_kick(kicked, 267)
            wait_for_workers()
        close.assert_called_once_with(1)
        launch.assert_called_once_with("FarmAlt")
        kinds = [event["kind"] for event in roblox_monitor.state["events"]]
        self.assertEqual(kinds, ["kick", "rejoined"])

    def test_account_already_back_is_not_launched_again(self):
        kicked = roblox_monitor.Client(1, os.devnull)
        kicked.user_id, kicked.joined_at = 111, time.monotonic()
        roblox_monitor.clients[1] = kicked
        with mock.patch.object(roblox_monitor, "close_client"), \
                mock.patch.object(roblox_monitor, "launch_account") as launch, \
                mock.patch.object(roblox_monitor, "REJOIN_DELAY", 0.3), \
                mock.patch.object(roblox_monitor, "wait_for_internet", return_value=True):
            roblox_monitor._on_kick(kicked, 267)
            back = roblox_monitor.Client(4, os.devnull)
            back.user_id, back.joined_at = 111, time.monotonic()
            roblox_monitor.clients[4] = back
            wait_for_workers()
        launch.assert_not_called()

    def test_socket_command_requires_matching_auth(self):
        with mock.patch.object(roblox_monitor, "SOCKET_AUTH", "secret"):
            self.assertEqual(roblox_monitor._decode_command("secret:GET_STATE"), "GET_STATE")
            self.assertIsNone(roblox_monitor._decode_command("wrong:GET_STATE"))
            self.assertIsNone(roblox_monitor._decode_command("GET_STATE"))

    def test_launch_refuses_anything_but_a_username(self):
        self.assertEqual(roblox_monitor.launch_account("x'; Remove-Item C:\\")[0], False)


class ConfigTests(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "settings.json")
            with mock.patch.dict(os.environ, {"ROBLOX_REJOINER_CONFIG": path}):
                settings = config_manager.DEFAULT_CONFIG.copy()
                settings["PLACE_ID"] = "987654"
                settings["SOCKET_AUTH"] = "test-auth"
                self.assertTrue(config_manager.save_config(settings))
                self.assertEqual(config_manager.load_config()["PLACE_ID"], "987654")
                self.assertFalse(os.path.exists(f"{path}.tmp"))


if __name__ == "__main__":
    unittest.main()
