import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class FakeButton:
    def __init__(self, pin, pull_up=True, bounce_time=0.01):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.when_pressed = None


class FakeRGBLED:
    def __init__(self, r, g, b, active_high=False):
        self.pins = (r, g, b)
        self.active_high = active_high
        self.color = (0.0, 0.0, 0.0)
        self.is_on = False

    def on(self):
        self.is_on = True

    def off(self):
        self.is_on = False
        self.color = (0.0, 0.0, 0.0)


class FakeMQTTClient:
    pass


class FakeFuture:
    def exception(self):
        return None

    def add_done_callback(self, cb):
        cb(self)


class FakeLoop:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        coro.close()
        task = mock.Mock()
        task.done.return_value = False
        self.tasks.append(task)
        return task


def load_mqtt_module():
    amqtt = types.ModuleType("amqtt")
    amqtt_client = types.ModuleType("amqtt.client")
    amqtt_client.MQTTClient = FakeMQTTClient
    amqtt.client = amqtt_client

    gpiozero = types.ModuleType("gpiozero")
    gpiozero.Button = FakeButton
    gpiozero.RGBLED = FakeRGBLED

    with mock.patch.dict(sys.modules, {
        "amqtt": amqtt,
        "amqtt.client": amqtt_client,
        "gpiozero": gpiozero,
    }):
        module_path = Path(__file__).resolve().parents[1] / "src" / "mqtt_client.py"
        spec = importlib.util.spec_from_file_location("mqtt_client", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module


MQTT_MODULE = load_mqtt_module()


class TestMqttClientUtils(unittest.TestCase):
    def setUp(self):
        MQTT_MODULE.config = {}
        MQTT_MODULE.controller = None
        MQTT_MODULE.client = object()

    def test_lire_and_ecrire_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            MQTT_MODULE.config_file = str(config_path)

            data = {"valid_color": [0, 255, 0], "idle": True}
            MQTT_MODULE.ecrire_config(data)

            loaded = MQTT_MODULE.lire_config()
            self.assertEqual(data, loaded)

    def test_lire_config_returns_empty_dict_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            MQTT_MODULE.config_file = str(Path(tmpdir) / "missing.json")
            self.assertEqual({}, MQTT_MODULE.lire_config())

    def test_parse_json_or_none(self):
        self.assertEqual({"a": 1}, MQTT_MODULE.parse_json_or_none('{"a": 1}'))
        self.assertEqual({"b": 2}, MQTT_MODULE.parse_json_or_none(b'{"b": 2}'))
        self.assertIsNone(MQTT_MODULE.parse_json_or_none("not-json"))
        self.assertIsNone(MQTT_MODULE.parse_json_or_none(b"\xff"))


class TestHandleMessage(unittest.TestCase):
    def setUp(self):
        MQTT_MODULE.config = {
            "blocked_color": [255, 0, 0],
            "valid_color": [0, 255, 0],
            "idle": False,
        }

    def test_handle_message_updates_config_and_persists(self):
        payload = json.dumps(
            {
                "blocked_color": [1, 2, 3],
                "valid_color": [4, 5, 6],
                "idle": True,
            }
        )

        with mock.patch.object(MQTT_MODULE, "ecrire_config") as write_mock:
            MQTT_MODULE.handle_message(payload, "buzzer/config")

        self.assertEqual([1, 2, 3], MQTT_MODULE.config["blocked_color"])
        self.assertEqual([4, 5, 6], MQTT_MODULE.config["valid_color"])
        self.assertTrue(MQTT_MODULE.config["idle"])
        write_mock.assert_called_once_with(MQTT_MODULE.config)

    def test_handle_message_control_dispatches_actions(self):
        controller = mock.Mock()
        MQTT_MODULE.controller = controller

        payload = json.dumps({"release": "", "lock": [1, 3], "unlock": [3]})
        MQTT_MODULE.handle_message(payload, "buzzer/control")

        controller.release.assert_called_once_with(None)
        controller.lock.assert_called_once_with([1, 3])
        controller.unlock.assert_called_once_with([3])

    def test_handle_message_ignores_invalid_json(self):
        controller = mock.Mock()
        MQTT_MODULE.controller = controller

        MQTT_MODULE.handle_message("{bad", "buzzer/control")

        controller.release.assert_not_called()
        controller.lock.assert_not_called()
        controller.unlock.assert_not_called()

    def test_handle_message_control_optional_flags(self):
        controller = mock.Mock()
        MQTT_MODULE.controller = controller

        payload = json.dumps({"start": True, "block": True, "shameThem": True})
        with mock.patch("builtins.print") as print_mock:
            MQTT_MODULE.handle_message(payload, "buzzer/control")

        printed = [args[0] for args, _kwargs in print_mock.call_args_list if args]
        self.assertIn("activated", printed)
        self.assertIn("blocked", printed)
        self.assertIn("allRed", printed)


class TestAsyncFunctions(unittest.IsolatedAsyncioTestCase):
    async def test_publish_buzzer(self):
        client = mock.AsyncMock()
        await MQTT_MODULE.publish_buzzer(client, 2)
        client.publish.assert_awaited_once_with("buzzer/pressed", b'{"pressed": 3}', qos=1)

    async def test_mqtt_client_connect_subscribe_deliver_then_cancel(self):
        class Message:
            def __init__(self, payload, topic):
                self.publish_packet = types.SimpleNamespace(
                    payload=types.SimpleNamespace(data=payload),
                    variable_header=types.SimpleNamespace(topic_name=topic),
                )

        class Client:
            def __init__(self):
                self.connect = mock.AsyncMock()
                self.subscribe = mock.AsyncMock()
                self.disconnect = mock.AsyncMock()
                self._calls = 0

            async def deliver_message(self):
                self._calls += 1
                if self._calls == 1:
                    return Message(b'{"release": ""}', "buzzer/control")
                raise asyncio.CancelledError()

        fake_client = Client()
        controller = mock.Mock()
        MQTT_MODULE.controller = controller

        with mock.patch.object(MQTT_MODULE, "MQTTClient", return_value=fake_client):
            await MQTT_MODULE.mqtt_client()

        fake_client.connect.assert_awaited_once_with("mqtt://localhost:1883/")
        self.assertEqual(3, fake_client.subscribe.await_count)
        fake_client.disconnect.assert_awaited_once()
        controller.release.assert_called_once_with(None)

    async def test_mqtt_client_retries_after_exception(self):
        class Client:
            def __init__(self):
                self.connect_attempts = 0
                self.disconnect = mock.AsyncMock()
                self.subscribe = mock.AsyncMock()

            async def connect(self, _url):
                self.connect_attempts += 1
                if self.connect_attempts == 1:
                    raise RuntimeError("boom")
                raise asyncio.CancelledError()

        fake_client = Client()

        with (
            mock.patch.object(MQTT_MODULE, "MQTTClient", return_value=fake_client),
            mock.patch("asyncio.sleep", new=mock.AsyncMock()) as sleep_mock,
        ):
            await MQTT_MODULE.mqtt_client()

        sleep_mock.assert_awaited_once_with(5)
        fake_client.disconnect.assert_awaited_once()


class TestButtonController(unittest.TestCase):
    def setUp(self):
        MQTT_MODULE.config = {
            "blocked_color": [255, 0, 0],
            "valid_color": [0, 255, 0],
            "idle": False,
        }
        MQTT_MODULE.client = object()

    def test_init_requires_led_triplets(self):
        with self.assertRaises(ValueError):
            MQTT_MODULE.ButtonController([17], [1, 2], FakeLoop())

    def test_lock_unlock_release_and_set_light(self):
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())

        controller.lock([2])
        self.assertEqual([1], controller.locked_array)

        controller.unlock([2])
        self.assertEqual([], controller.locked_array)

        controller.set_light(True)
        self.assertTrue(all(led.is_on for led in controller.leds))

        controller.set_light(False, index=1)
        self.assertFalse(controller.leds[1].is_on)

        controller.locked = True
        controller.active_led_index = 0
        controller.release(None)
        self.assertFalse(controller.locked)
        self.assertIsNone(controller.active_led_index)
        self.assertTrue(all(led.color == (0.0, 0.0, 0.0) for led in controller.leds))

    def test_release_with_indices_only(self):
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())
        controller.leds[0].on()
        controller.leds[1].on()
        controller.locked = True

        controller.release([1, 99])

        self.assertFalse(controller.leds[0].is_on)
        self.assertTrue(controller.leds[1].is_on)
        self.assertTrue(controller.locked)
        self.assertIsNone(controller.active_led_index)

    def test_release_restarts_idle_when_enabled(self):
        loop = FakeLoop()
        MQTT_MODULE.config = {"idle": True, "blocked_color": [255, 0, 0], "valid_color": [0, 255, 0]}
        controller = MQTT_MODULE.ButtonController([17], [1, 2, 3], loop)
        controller.idle_task = mock.Mock()
        controller.idle_task.done.return_value = True

        controller.release(None)

        self.assertEqual(2, len(loop.tasks))

    def test_handle_button_press_sets_state_and_colors(self):
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())

        def fake_run_coroutine_threadsafe(coro, _loop):
            coro.close()
            return FakeFuture()

        with mock.patch.object(MQTT_MODULE, "run_coroutine_threadsafe", side_effect=fake_run_coroutine_threadsafe):
            controller.handle_button_press(1)

        self.assertTrue(controller.locked)
        self.assertEqual(1, controller.active_led_index)
        self.assertEqual((0.0, 1.0, 0.0), controller.leds[1].color)
        self.assertEqual((1.0, 0.0, 0.0), controller.leds[0].color)

    def test_handle_button_press_cancels_idle_task(self):
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())
        task = mock.Mock()
        task.done.return_value = False
        controller.idle_task = task

        def fake_run_coroutine_threadsafe(coro, _loop):
            coro.close()
            return FakeFuture()

        with mock.patch.object(MQTT_MODULE, "run_coroutine_threadsafe", side_effect=fake_run_coroutine_threadsafe):
            controller.handle_button_press(0)

        task.cancel.assert_called_once()

    def test_handle_button_press_noop_when_locked_or_disabled(self):
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())
        controller.locked = True

        with mock.patch.object(MQTT_MODULE, "run_coroutine_threadsafe") as publish_mock:
            controller.handle_button_press(0)

        publish_mock.assert_not_called()

    def test_idle_animation_start_and_helpers(self):
        loop = FakeLoop()
        MQTT_MODULE.config = {"idle": True, "blocked_color": [255, 0, 0], "valid_color": [0, 255, 0]}
        controller = MQTT_MODULE.ButtonController([17], [1, 2, 3], loop)
        self.assertEqual(1, len(loop.tasks))

        done_task = mock.Mock()
        done_task.done.return_value = True
        controller.idle_task = done_task
        controller.start_idle_animation()
        self.assertEqual(2, len(loop.tasks))

        self.assertTrue(controller._valid_led_index(1))
        self.assertFalse(controller._valid_led_index(0))
        self.assertFalse(controller._valid_led_index("1"))
        r, g, b = controller.hsv_to_rgb(0.5, 1.0, 1.0)
        self.assertTrue(all(0.0 <= c <= 1.0 for c in (r, g, b)))

    def test_cleanup_turns_all_leds_off(self):
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())
        controller.set_light(True)
        controller.cleanup()
        self.assertTrue(all(not led.is_on for led in controller.leds))


class TestIdleAnimation(unittest.IsolatedAsyncioTestCase):
    async def test_idle_animation_exits_and_turns_leds_off_when_locked(self):
        MQTT_MODULE.config = {"idle": False, "blocked_color": [255, 0, 0], "valid_color": [0, 255, 0]}
        controller = MQTT_MODULE.ButtonController([17], [1, 2, 3], FakeLoop())
        controller.leds[0].on()
        controller.locked = True

        await controller._idle_animation()

        self.assertFalse(controller.leds[0].is_on)

    async def test_idle_animation_runs_one_iteration(self):
        MQTT_MODULE.config = {"idle": False, "blocked_color": [255, 0, 0], "valid_color": [0, 255, 0]}
        controller = MQTT_MODULE.ButtonController([17, 27], [1, 2, 3, 4, 5, 6], FakeLoop())
        observed_colors = []

        async def stop_after_one_tick(_delay):
            observed_colors.extend([led.color for led in controller.leds])
            controller.locked = True

        with mock.patch("asyncio.sleep", side_effect=stop_after_one_tick):
            await controller._idle_animation()

        self.assertEqual(2, len(observed_colors))
        self.assertTrue(all(len(color) == 3 for color in observed_colors))
        self.assertTrue(all(not led.is_on for led in controller.leds))

        controller.locked = False
        controller.locked_array = [0]

        with mock.patch.object(MQTT_MODULE, "run_coroutine_threadsafe") as publish_mock:
            controller.handle_button_press(0)

        publish_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
