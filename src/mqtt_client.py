import asyncio
from asyncio import run_coroutine_threadsafe
from amqtt.client import MQTTClient
import json
from gpiozero import Button, LED, RGBLED

# Chemin vers le fichier de configuration JSON
config_file = 'config.json'
client = MQTTClient()
config = {}
controller: "ButtonController|None" = None
locked_array: []


# Lecture du fichier JSON
def lire_config():
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print("Fichier de configuration non trouvé, création d'un fichier vide.")
        return {}


# Écriture dans le fichier JSON
def ecrire_config(config):
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)


async def publish_buzzer(client, index):
    payload = str(index + 1).encode("utf-8")
    await client.publish('buzzer/pressed', payload, qos=1)


class ButtonController:
    def __init__(self, input_pins, led_pins, loop):
        self.input_pins = list(dict.fromkeys(input_pins))
        self.led_pins = led_pins
        self.loop = loop
        self.locked_array = list()

        # Boutons en pull-down (pull_up=False). Anti-rebond ~200 ms
        self.buttons = []
        for idx, pin in enumerate(self.input_pins):
            b = Button(pin, pull_up=True, bounce_time=0.01)
            b.when_pressed = (lambda i=idx: self.handle_button_press(i))
            self.buttons.append(b)

        # LEDs
        self.leds = [RGBLED(self.led_pins[p * 3], self.led_pins[p * 3 + 1], self.led_pins[p * 3 + 2], active_high=False)
                     for p in range(int(len(self.led_pins) / 3))]
        self.locked = False
        self.active_led_index = None
        self.lock_timer = None
        self.idle_task = None

        # Démarre le mode idle
        if config["idle"]:
            self.start_idle_animation()

    def start_idle_animation(self):
        """Lance la coroutine idle en tâche de fond"""
        if self.idle_task is None or self.idle_task.done():
            self.idle_task = self.loop.create_task(self._idle_animation())

    async def _idle_animation(self):
        """Arc-en-ciel fluide tant qu’aucun buzzer n’est actif"""
        hue = 0
        while not self.locked:
            hue = (hue + 2) % 360
            for i, led in enumerate(self.leds):
                # déphase légèrement chaque LED pour un effet circulaire
                offset_hue = (hue + i * 30) % 360
                r, g, b = self.hsv_to_rgb(offset_hue / 360, 1.0, 0.2)
                led.color = (r, g, b)
            await asyncio.sleep(0.01)  # 50 ms → ~20 fps

        # quand on sort (verrou activé), on éteint tout
        for led in self.leds:
            led.off()

    def handle_button_press(self, index):
        print(f"[DEBUG] PRESS index={index} gpio={self.input_pins[index]}")

        blocked_color = tuple(c/255 for c in config["blocked_color"])
        valid_color = tuple(c/255 for c in config["valid_color"])

        if self.locked or index in self.locked_array:
            return

        self.locked = True
        # print(f"Le buzzer {index + 1} a appuyé.")

        fut = run_coroutine_threadsafe(publish_buzzer(client, index), self.loop)
        fut.add_done_callback(lambda f: print("[MQTT OK] publish") if not f.exception() else print("[MQTT ERR]", f.exception()))

        self.active_led_index = index
        # stoppe le mode idle
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()

        for ind in range(len(self.input_pins)):
            self.leds[ind].color = valid_color if ind == index else blocked_color

    def hsv_to_rgb(self, h, s, v):
        """Convertit une teinte [0–1] HSV en RGB [0–1]"""
        import colorsys
        return colorsys.hsv_to_rgb(h, s, v)

    def release(self, indices):
        if indices is not None and len(indices) > 0:
            for led_ind in indices:
                self.leds[led_ind - 1].off()
        else:
            for led in self.leds:
                led.off()
            self.locked = False
        self.active_led_index = None
        if config["idle"]:
            self.start_idle_animation()

    def lock(self, lock_array):
        if lock_array is not None:
            for led_ind in lock_array:
                self.leds[led_ind - 1].off()
                if (led_ind - 1) not in self.locked_array:
                    self.locked_array.append(led_ind - 1)

    def unlock(self, unlock_array):
        if unlock_array is not None:
            for led_ind in unlock_array:
                self.leds[led_ind - 1].off()
                if (led_ind - 1) in self.locked_array:
                    del self.locked_array[self.locked_array.index(led_ind - 1)]

    def cleanup(self):
        for led in self.leds:
            led.off()

    def set_light(self, on: bool, index: int | None = None):
        """Allume/éteint une LED (ou toutes si index=None) sans affecter le verrou logique."""
        if index is None:
            for led in self.leds:
                (led.on() if on else led.off())
        else:
            if 0 <= index < len(self.leds):
                (self.leds[index].on() if on else self.leds[index].off())


# fin classe


def is_json(myjson):
    try:
        json.loads(myjson)
    except ValueError as e:
        return False
    return True


def handle_message(data, topic):
    global config, controller
    print(f"Received message: {data} on topic: {topic}")
    if is_json(data):
        message = json.loads(data)
        if topic == "buzzer/config":
            if "blocked_color" in message:
                config["blocked_color"] = message["blocked_color"]
            if "valid_color" in message:
                config["valid_color"] = message["valid_color"]
            if "idle" in message:
                config["idle"] = message["idle"]
            ecrire_config(config)
        elif topic == "buzzer/control":
            if "release" in message:
                controller.release(None if message["release"] == "" else message["release"])
            if "lock" in message:
                controller.lock(message["lock"])
            if "unlock" in message:
                controller.unlock(message["unlock"])
            if "start" in message:
                print("activated")
            if "block" in message:
                print("blocked")
            if "shameThem" in message:
                print("allRed")


async def mqtt_client():
    global client
    client = MQTTClient()
    while True:
        try:
            print("Tentative de connexion au broker MQTT...")
            await client.connect('mqtt://localhost:1883/')
            print("Connecté au broker MQTT")

            await client.subscribe([('buzzer/config', 1)])
            print("Abonné au topic 'buzzer/config'")

            await client.subscribe([('buzzer/control', 1)])
            print("Abonné au topic 'buzzer/control'")

            await client.subscribe([('buzzer/pressed', 1)])
            print("Abonné au topic 'buzzer/pressed'")

            while True:
                message = await client.deliver_message()
                packet = message.publish_packet
                handle_message(packet.payload.data, packet.variable_header.topic_name)

        except Exception as e:
            print(f"Erreur MQTT : {e}, nouvelle tentative dans 5 secondes...")
            await asyncio.sleep(5)

        except asyncio.CancelledError:
            await client.disconnect()
            print("Déconnexion MQTT propre")
            break


if __name__ == '__main__':
    config = lire_config()
    print("Config actuelle :", config)

    loop = asyncio.get_event_loop()

    # démarrage MQTT en tâche de fond
    task = loop.create_task(mqtt_client())

    # instancie le contrôleur (garde la même loop pour run_coroutine_threadsafe)
    controller = ButtonController(config.get('input_pins', []), config.get('led_pins', []), loop)

    try:
        # lance la boucle asyncio (nécessaire !)
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # arrêt propre
        if controller:
            controller.cleanup()
        if not task.done():
            task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        loop.stop()
        loop.close()
        print("Programme arrêté proprement")
