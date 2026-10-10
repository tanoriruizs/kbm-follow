"""Pruebas que no necesitan hardware: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar, List, Mapping
from unittest import mock

from kbm_follow import drivers
from kbm_follow.agent import Agent, Coordinator, switch_followers
from kbm_follow.config import Config, FollowerConfig, LeaderConfig
from kbm_follow.drivers import Follower, FollowerDriver, SwitchResult, create_follower
from kbm_follow.errors import KbmError
from kbm_follow.leader import Debouncer, device_path_pattern
from kbm_follow.net import (KIND_LEADER_ARRIVED, InvalidMessage, Message, PeerBus, ReplayGuard,
                            decode, encode)

SECRET = "s" * 24


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# Driver de mentira: anota a qué canal le pidieron ir y puede fallar a propósito.
class RecordingDriver(FollowerDriver):
    OPTIONS: ClassVar[Mapping[str, type]] = {"match": str, "fail_times": int}
    REQUIRED_OPTIONS: ClassVar[frozenset] = frozenset({"match"})

    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        self.calls: List[int] = []
        self._failures_left = self.options.get("fail_times", 0)

    @property
    def description(self) -> str:
        return f"recording '{self.options['match']}'"

    def switch_to(self, channel: int) -> SwitchResult:
        self.calls.append(channel)
        if self._failures_left:
            self._failures_left -= 1
            raise KbmError("simulated failure")
        return SwitchResult.SWITCHED


def make_config(**overrides: Any) -> Config:
    values: dict = dict(channel=1, secret=SECRET, name="test",
                        leader=LeaderConfig("F75", "05AC:024F"),
                        followers=[FollowerConfig("logitech-hidpp", {"match": "MX Master 3S"})])
    values.update(overrides)
    return Config(**values)


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.switches: List[int] = []
        self.announcements: List[bool] = []
        self.coord = Coordinator(1, 8.0, self.switches.append, self.announcements.append, self.clock)

    def test_initial_state_does_not_announce(self) -> None:
        self.coord.leader_changed(True)
        self.assertEqual(self.announcements, [])

    def test_arrival_from_elsewhere_is_not_reconnect(self) -> None:
        self.coord.leader_changed(False)
        self.coord.remote_arrival(2, "mac")
        self.coord.leader_changed(True)
        self.assertEqual(self.announcements, [False])

    def test_wake_up_here_is_reconnect(self) -> None:
        self.coord.leader_changed(True)
        self.coord.leader_changed(False)
        self.coord.leader_changed(True)
        self.assertEqual(self.announcements, [True])
        self.assertEqual(self.switches, [])

    def test_remote_arrival_after_local_departure(self) -> None:
        self.coord.leader_changed(True)
        self.coord.leader_changed(False)
        self.coord.remote_arrival(2, "mac")
        self.assertEqual(self.switches, [2])

    def test_remote_arrival_before_local_departure_waits(self) -> None:
        self.coord.leader_changed(True)
        self.coord.remote_arrival(2, "mac")
        self.assertEqual(self.switches, [])
        self.clock.now = 1.5
        self.coord.leader_changed(False)
        self.assertEqual(self.switches, [2])

    def test_stale_pending_arrival_is_ignored(self) -> None:
        self.coord.leader_changed(True)
        self.coord.remote_arrival(2, "mac")
        self.clock.now = 60
        self.coord.leader_changed(False)
        self.assertEqual(self.switches, [])

    def test_remote_reconnect_never_moves_followers(self) -> None:
        self.coord.leader_changed(False)
        self.coord.remote_arrival(2, "mac", reconnect=True)
        self.assertEqual(self.switches, [])

    def test_switch_while_leader_was_asleep(self) -> None:
        self.coord.leader_changed(True)
        self.coord.leader_changed(False)  # se durmió hace rato
        self.clock.now = 600
        self.coord.remote_arrival(2, "mac")  # y lo despertaron directo en el canal 2
        self.assertEqual(self.switches, [2])

    def test_own_channel_is_ignored(self) -> None:
        self.coord.leader_changed(False)
        self.coord.remote_arrival(1, "other")
        self.assertEqual(self.switches, [])

    def test_unknown_state_switches_immediately(self) -> None:
        self.coord.remote_arrival(2, "mac")
        self.assertEqual(self.switches, [2])


class DebouncerTests(unittest.TestCase):
    def test_requires_consecutive_readings(self) -> None:
        deb = Debouncer(2)
        self.assertIsNone(deb.feed(True))
        self.assertEqual(deb.feed(True), (None, True))
        self.assertIsNone(deb.feed(False))
        self.assertIsNone(deb.feed(True))  # rebote
        self.assertIsNone(deb.feed(False))
        self.assertEqual(deb.feed(False), (True, False))


class PathPatternTests(unittest.TestCase):
    KEYBOARD = (r"\\?\HID#{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&0105ac_PID&024f_REV&0138"
                r"_dc075b1b980f&Col01#9&1bb48239&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}\KBD")
    MOUSE = (r"\\?\HID#{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&02046d_PID&b034_REV&0009"
             r"_d93d97564b5c&Col01#9&365556&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}")

    def test_matches_ble_device_only(self) -> None:
        pattern = device_path_pattern(0x05AC, 0x024F)
        self.assertTrue(pattern.search(self.KEYBOARD))
        self.assertFalse(pattern.search(self.MOUSE))

    def test_matches_usb_path(self) -> None:
        pattern = device_path_pattern(0x0C45, 0x800A)
        self.assertTrue(pattern.search(r"\\?\HID#VID_0C45&PID_800A&MI_00#7&2&0&0000#{x}"))


class MessageTests(unittest.TestCase):
    def _message(self) -> Message:
        return Message.new(KIND_LEADER_ARRIVED, "id", "mac", 2)

    def test_roundtrip(self) -> None:
        message = self._message()
        self.assertEqual(decode(encode(message, SECRET.encode()), SECRET.encode()), message)

    def test_wrong_secret_rejected(self) -> None:
        with self.assertRaises(InvalidMessage):
            decode(encode(self._message(), SECRET.encode()), b"another-different-secret")

    def test_tampered_body_rejected(self) -> None:
        envelope = json.loads(encode(self._message(), SECRET.encode()))
        envelope["body"] = envelope["body"].replace('"channel":2', '"channel":3')
        with self.assertRaises(InvalidMessage):
            decode(json.dumps(envelope).encode(), SECRET.encode())

    def test_stale_message_rejected(self) -> None:
        data = encode(self._message(), SECRET.encode())
        with self.assertRaises(InvalidMessage):
            decode(data, SECRET.encode(), now=time.time() + 3600)

    def test_garbage_rejected(self) -> None:
        for data in (b"", b"\xff\xfe", b"[]", b'{"body":1,"sig":2}'):
            with self.assertRaises(InvalidMessage):
                decode(data, SECRET.encode())

    def test_replay_guard(self) -> None:
        guard = ReplayGuard(capacity=2)
        self.assertTrue(guard.first_time("a"))
        self.assertFalse(guard.first_time("a"))


class ConfigTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        config = make_config(followers=[FollowerConfig("logitech-hidpp", {"match": "MX"}, {1: 2})])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config.save(path)
            loaded = Config.load(path)
        self.assertEqual(loaded.to_dict(), config.to_dict())
        self.assertEqual(loaded.leader.windows_id_tuple, (0x05AC, 0x024F))
        self.assertEqual(loaded.followers[0].channel_map, {1: 2})

    def test_rejects_invalid_files(self) -> None:
        base = make_config().to_dict()
        cases = [
            {**base, "typo": 1},
            {**base, "channel": 9},
            {**base, "secret": "short"},
            {**base, "followers": []},
            {**base, "leader": {"match": ""}},
            {**base, "leader": {"match": "F75", "windows_id": "nope"}},
            {**base, "followers": [{"match": "no driver"}]},
            {**base, "followers": [{"driver": "x", "channel_map": {"1": 99}}]},
            {**base, "version": 99},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for case in cases:
                path.write_text(json.dumps(case))
                with self.subTest(case=case), self.assertRaises(KbmError):
                    Config.load(path)


class DriverTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(drivers._BUILTIN_DRIVERS,
                                  {"recording": f"{__name__}:RecordingDriver"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_builtin_logitech_driver_is_available(self) -> None:
        self.assertIn("logitech-hidpp", drivers.available_drivers())
        follower = create_follower("logitech-hidpp", {"match": "MX Master 3S"})
        self.assertEqual(follower.description, "Logitech 'MX Master 3S'")

    def test_unknown_driver(self) -> None:
        with self.assertRaises(KbmError):
            create_follower("does-not-exist", {})

    def test_option_validation(self) -> None:
        for options in ({}, {"match": 3}, {"match": "x", "extra": 1}, {"match": "x", "fail_times": True}):
            with self.subTest(options=options), self.assertRaises(KbmError):
                create_follower("recording", options)

    def test_channel_map(self) -> None:
        follower = create_follower("recording", {"match": "x"}, {2: 3})
        follower.switch_to(2)
        follower.switch_to(1)
        self.assertEqual(follower.driver.calls, [3, 1])

    def test_switch_followers_retries_and_isolates_failures(self) -> None:
        flaky = create_follower("recording", {"match": "flaky", "fail_times": 1})
        broken = create_follower("recording", {"match": "broken", "fail_times": 5})
        healthy = create_follower("recording", {"match": "ok"})
        with mock.patch("kbm_follow.agent._SWITCH_RETRY_DELAY_S", 0):
            switch_followers([flaky, broken, healthy], 2)
        self.assertEqual(flaky.driver.calls, [2, 2])
        self.assertEqual(broken.driver.calls, [2, 2])
        self.assertEqual(healthy.driver.calls, [2])


class FakeDetector:
    description = "fake"

    def __init__(self, present: bool) -> None:
        self.value = present

    def present(self) -> bool:
        return self.value


class TwoAgentsOverLoopbackTest(unittest.TestCase):
    """Dos agentes de verdad hablando por UDP en 127.0.0.1: el líder va a la compu 2 y regresa."""

    PORT_1, PORT_2 = 47911, 47912

    def _agent(self, channel: int, port: int, peer_port: int, detector: FakeDetector) -> tuple:
        config = make_config(channel=channel, name=f"pc{channel}", port=port, broadcast=False,
                             peers=["127.0.0.1"], poll_interval_s=0.05)
        bus = PeerBus(SECRET, port, ["127.0.0.1"], broadcast=False, send_port=peer_port)
        follower = Follower(RecordingDriver({"match": f"mouse@pc{channel}"}))
        return Agent(config, detector=detector, bus=bus, followers=[follower]), follower.driver.calls

    def test_followers_follow_the_leader(self) -> None:
        leader_on_1, leader_on_2 = FakeDetector(True), FakeDetector(False)
        agent1, calls1 = self._agent(1, self.PORT_1, self.PORT_2, leader_on_1)
        agent2, calls2 = self._agent(2, self.PORT_2, self.PORT_1, leader_on_2)
        threads = [threading.Thread(target=a.run, daemon=True) for a in (agent1, agent2)]
        for thread in threads:
            thread.start()
        try:
            time.sleep(0.4)  # dejamos que ambos registren el estado inicial
            leader_on_1.value, leader_on_2.value = False, True  # el líder se va a la compu 2
            self._wait_for(lambda: calls1 == [2])
            self.assertEqual(calls2, [])

            leader_on_2.value = False  # se duerme y despierta en la compu 2: nada debe moverse
            time.sleep(0.3)
            leader_on_2.value = True
            time.sleep(0.5)
            self.assertEqual(calls1, [2])
            self.assertEqual(calls2, [])

            leader_on_2.value, leader_on_1.value = False, True  # regresa a la compu 1
            self._wait_for(lambda: calls2 == [1])
        finally:
            agent1.stop()
            agent2.stop()
            for thread in threads:
                thread.join(timeout=3)

    @staticmethod
    def _wait_for(condition, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError("condition not met in time")


if __name__ == "__main__":
    unittest.main()
