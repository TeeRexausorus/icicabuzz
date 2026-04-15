import asyncio
import json
import time
from asyncio import run_coroutine_threadsafe

import spidev
from amqtt.client import MQTTClient
from gpiozero import Button, OutputDevice

# Chemin vers le fichier de configuration JSON
config_file = '/opt/mqttPython/src/config.json'
client = None
config = {}
controller: "ButtonController|None" = None


# Lecture du fichier JSON
def lire_config():
    global config
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        print("Fichier de configuration non trouve, creation d'un fichier vide.")
        return {}


# Ecriture dans le fichier JSON
def ecrire_config(new_config):
    with open(config_file, 'w') as f:
        json.dump(new_config, f, indent=2)


async def publish_buzzer(pub_client, index):
    payload = json.dumps({'pressed': int(index + 1)})
    await pub_client.publish('buzzer/pressed', payload.encode("utf-8"), qos=1, retain=True)


class ButtonController:
    # 74HC595D
    SR_DATA_PIN = 23  # pin physique 16 (SER / SR_IN)
    SR_CLOCK_PIN = 24  # pin physique 18 (SH_CP)
    SR_LATCH_PIN = 25  # pin physique 22 (ST_CP)
    SR_OE_PIN = 18  # pin physique 12 (OE#, actif bas)

    # MCP23S08
    MCP_SPI_BUS = 0
    MCP_SPI_DEVICE = 0  # CE0, pin physique 24
    MCP_INT_PIN = 17  # pin physique 11

    # 2 x 74HC595 -> 16 sorties (5 buzzers RGB = 15 sorties)
    SR_TOTAL_BITS = 24

    def __init__(self, buzzer_count, idle_loop):
        self.buzzer_count = int(buzzer_count)
        if self.buzzer_count * 3 > self.SR_TOTAL_BITS:
            raise ValueError("Nombre de buzzers trop grand pour 2x74HC595 (16 sorties).")
        self.loop = idle_loop
        self.locked_array = list()
        self.input_bits = list(range(min(8, self.buzzer_count)))
        self.last_pressed_at = {}
        self.debounce_s = 0.05
        self.debug_mcp = bool(config.get("mcp_debug", False))
        self._debug_task = None

        # 74HC595 GPIO
        self.sr_data = OutputDevice(self.SR_DATA_PIN, active_high=True, initial_value=False)
        self.sr_clock = OutputDevice(self.SR_CLOCK_PIN, active_high=True, initial_value=False)
        self.sr_latch = OutputDevice(self.SR_LATCH_PIN, active_high=True, initial_value=False)
        # OE# actif bas: on met à 0 pour activer les sorties
        self.sr_oe = OutputDevice(self.SR_OE_PIN, active_high=True, initial_value=False)
        self.sr_state = [False] * self.SR_TOTAL_BITS

        # MCP23S08 (SPI0 CE0)
        self.spi = spidev.SpiDev()
        try:
            self.spi.open(self.MCP_SPI_BUS, self.MCP_SPI_DEVICE)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "SPI device introuvable (/dev/spidev0.0). Active SPI "
                "(sudo raspi-config nonint do_spi 0), puis redémarre."
            ) from exc
        self.spi.max_speed_hz = 1_000_000
        self.spi.mode = 0b00
        self.mcp_int = Button(self.MCP_INT_PIN, pull_up=True, bounce_time=0.001)
        self.mcp_int.when_pressed = self._on_mcp_interrupt
        self._setup_mcp23s08()
        self._log_mcp_setup()
        if self.debug_mcp:
            print("[MCP23S08] debug polling enabled (config.mcp_debug=true)")
            self._debug_task = self.loop.create_task(self._debug_poll_inputs())
        self.locked = False
        self.active_led_index = None
        self.lock_timer = None
        self.idle_task = None

        # Demarre le mode idle
        idle = config.get("idle", False)

        if isinstance(idle, list):
            idle_color = tuple(c / 255 for c in idle)
            self.start_idle_block(idle_color)
        elif idle is True:
            self.start_idle_animation()

    def start_idle_animation(self):
        """Lance la coroutine idle en tache de fond"""
        if self.idle_task is None or self.idle_task.done():
            self.idle_task = self.loop.create_task(self._idle_animation())

    def start_idle_block(self, color):
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        """Lance la coroutine idle en tache de fond"""
        if self.idle_task is None or self.idle_task.done():
            self.idle_task = self.loop.create_task(self._idle_block(color))

    async def _idle_animation(self):
        """Arc-en-ciel fluide tant qu'aucun buzzer n'est actif"""
        hue = 0
        while not self.locked:
            hue = (hue + 2) % 360
            for i in range(self.buzzer_count):
                if i in self.locked_array:
                    self._set_led_rgb(i, 0, 0, 0)
                    continue
                # Dephase legerement chaque LED pour un effet circulaire
                offset_hue = (hue + i * 30) % 360
                r, g, b = self.hsv_to_rgb(offset_hue / 360, 1.0, 0.2)
                self._set_led_color_float(i, (r, g, b))
            await asyncio.sleep(0.01)  # ~20 fps

        # quand on sort (verrou active), on eteint tout
        self._all_leds_off()

    async def _idle_block(self, color_tuple):
        while not self.locked:
            for i in range(self.buzzer_count):
                if i in self.locked_array:
                    self._set_led_rgb(i, 0, 0, 0)
                else:
                    self._set_led_color_float(i, color_tuple)
            await asyncio.sleep(0.01)  # ~20 fps

        # quand on sort (verrou active), on eteint tout
        self._all_leds_off()

    def handle_button_press(self, index):
        print(f"[BUTTON] press detected index={index + 1}")

        blocked_color = tuple(c / 255 for c in config["blocked_color"])
        valid_color = tuple(c / 255 for c in config["valid_color"])

        if self.locked or index in self.locked_array:
            print(f"[BUTTON] ignored index={index + 1} locked={self.locked} locked_array={self.locked_array}")
            return

        self.locked = True
        print(f"[BUTTON] accepted index={index + 1} -> publishing buzzer/pressed")

        fut = run_coroutine_threadsafe(publish_buzzer(client, index), self.loop)
        fut.add_done_callback(
            lambda f: print("[MQTT OK] publish") if not f.exception() else print("[MQTT ERR]", f.exception())
        )

        self.active_led_index = index

        # stoppe le mode idle
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()

        for ind in range(self.buzzer_count):
            if ind in self.locked_array:
                self._set_led_rgb(ind, 0, 0, 0)
            else:
                self._set_led_color_float(ind, valid_color if ind == index else blocked_color)

    @staticmethod
    def hsv_to_rgb(h, s, v):
        """Convertit une teinte [0-1] HSV en RGB [0-1]"""
        import colorsys
        return colorsys.hsv_to_rgb(h, s, v)

    def _valid_led_index(self, led_ind):
        return isinstance(led_ind, int) and 1 <= led_ind <= self.buzzer_count

    def release(self, indices):
        if indices is not None and len(indices) > 0:
            for led_ind in indices:
                if self._valid_led_index(led_ind):
                    self._set_led_rgb(led_ind - 1, 0, 0, 0)
        else:
            self._all_leds_off()
            self.locked = False
        self.active_led_index = None
        idle = config.get("idle", False)
        if isinstance(idle, list):
            idle_color = tuple(c / 255 for c in idle)
            self.start_idle_block(idle_color)
        elif idle is True:
            self.start_idle_animation()

    def lock(self, lock_array):
        if lock_array is None:
            return

        if len(lock_array) == 0:
            self.locked_array = list(range(self.buzzer_count))
            self._all_leds_off()
            return

        for led_ind in lock_array:
            if self._valid_led_index(led_ind):
                self._set_led_rgb(led_ind - 1, 0, 0, 0)
                if (led_ind - 1) not in self.locked_array:
                    self.locked_array.append(led_ind - 1)

    def unlock(self, unlock_array):
        if unlock_array is None:
            return

        if len(unlock_array) == 0:
            self.locked_array.clear()
            self._all_leds_off()
            return

        for led_ind in unlock_array:
            if self._valid_led_index(led_ind):
                self._set_led_rgb(led_ind - 1, 0, 0, 0)
                if (led_ind - 1) in self.locked_array:
                    self.locked_array.remove(led_ind - 1)

    def cleanup(self):
        self._all_leds_off()
        if self._debug_task and not self._debug_task.done():
            self._debug_task.cancel()
        self.mcp_int.close()
        self.spi.close()
        self.sr_data.close()
        self.sr_clock.close()
        self.sr_latch.close()
        self.sr_oe.close()

    def set_light(self, on: bool, index: int | None = None):
        """Allume/eteint une LED (ou toutes si index=None) sans affecter le verrou logique."""
        if index is None:
            for i in range(self.buzzer_count):
                self._set_led_rgb(i, 1 if on else 0, 1 if on else 0, 1 if on else 0)
        else:
            if 0 <= index < self.buzzer_count:
                self._set_led_rgb(index, 1 if on else 0, 1 if on else 0, 1 if on else 0)

    def _set_led_color_float(self, led_index, color_tuple):
        # 74HC595 = sortie numérique (pas de PWM), on seuillage les composantes.
        r = 1 if color_tuple[0] > 0.1 else 0
        g = 1 if color_tuple[1] > 0.1 else 0
        b = 1 if color_tuple[2] > 0.1 else 0
        self._set_led_rgb(led_index, r, g, b)

    def _set_led_rgb(self, led_index, r, g, b):
        base = led_index * 3
        if base + 2 >= len(self.sr_state):
            return
        self.sr_state[base] = bool(r)
        self.sr_state[base + 1] = bool(g)
        self.sr_state[base + 2] = bool(b)
        self._sr_flush()

    def _all_leds_off(self):
        for i in range(self.buzzer_count):
            base = i * 3
            if base + 2 < len(self.sr_state):
                self.sr_state[base] = False
                self.sr_state[base + 1] = False
                self.sr_state[base + 2] = False
        self._sr_flush()

    def _sr_flush(self):
        # Envoi MSB->LSB dans la chaine; inversion logique pour LED active low (common anode).
        self.sr_latch.off()
        for bit in reversed(self.sr_state):
            physical_level = 0 if bit else 1
            if physical_level:
                self.sr_data.on()
            else:
                self.sr_data.off()
            self.sr_clock.on()
            self.sr_clock.off()
        self.sr_latch.on()
        self.sr_latch.off()

    @staticmethod
    def _mcp_opcode(read):
        # 0x40 = 0b0100_0000 (addr 000, write), 0x41 en lecture
        return 0x41 if read else 0x40

    def _mcp_write(self, reg, value):
        self.spi.xfer2([self._mcp_opcode(False), reg & 0xFF, value & 0xFF])

    def _mcp_read(self, reg):
        resp = self.spi.xfer2([self._mcp_opcode(True), reg & 0xFF, 0x00])
        return resp[2]

    def _setup_mcp23s08(self):
        # Registres MCP23S08
        IODIR = 0x00
        GPINTEN = 0x02
        IOCON = 0x05
        GPPU = 0x06
        INTCAP = 0x08
        input_mask = 0
        for b in self.input_bits:
            input_mask |= (1 << b)

        self._mcp_write(IOCON, 0x00)
        self._mcp_write(IODIR, input_mask)  # bits en entrée
        self._mcp_write(GPPU, input_mask)  # pull-up interne
        self._mcp_write(GPINTEN, input_mask)  # interruption sur changement
        # Clear interruption latente
        self._mcp_read(INTCAP)

    def _log_mcp_setup(self):
        IODIR = 0x00
        GPINTEN = 0x02
        GPIO = 0x09
        GPPU = 0x06
        iodir = self._mcp_read(IODIR)
        gppu = self._mcp_read(GPPU)
        gpinten = self._mcp_read(GPINTEN)
        gpio = self._mcp_read(GPIO)
        print(
            f"[MCP23S08] setup iodir=0b{iodir:08b} gppu=0b{gppu:08b} "
            f"gpinten=0b{gpinten:08b} gpio=0b{gpio:08b}"
        )

    async def _debug_poll_inputs(self):
        GPIO = 0x09
        last_gpio = None
        while True:
            gpio = self._mcp_read(GPIO)
            if gpio != last_gpio:
                print(f"[MCP23S08] poll gpio=0b{gpio:08b}")
                last_gpio = gpio
            await asyncio.sleep(0.1)

    def _on_mcp_interrupt(self):
        INTF = 0x07
        INTCAP = 0x08
        intf = self._mcp_read(INTF)
        captured = self._mcp_read(INTCAP)  # lecture = acquitte interruption
        print(f"[MCP23S08] interrupt intf=0b{intf:08b} intcap=0b{captured:08b}")
        now = time.monotonic()

        for bit in self.input_bits:
            if not (intf & (1 << bit)):
                continue
            # Pull-up => bouton pressé = 0
            is_pressed = ((captured >> bit) & 0x01) == 0
            if not is_pressed:
                print(f"[BUTTON] bit={bit} edge ignored (release/no press)")
                continue
            last = self.last_pressed_at.get(bit, 0.0)
            if now - last < self.debounce_s:
                print(f"[BUTTON] bit={bit} debounce ignored delta={now - last:.4f}s")
                continue
            self.last_pressed_at[bit] = now
            print(f"[BUTTON] bit={bit} -> logical index={bit + 1} pressed")
            self.handle_button_press(bit)


# fin classe


def parse_json_or_none(payload):
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(e)
        return None


def handle_message(data, topic):
    global config, controller
    print(f"Received message: {data} on topic: {topic}")

    message = parse_json_or_none(data)
    if message is None:
        return

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
            print("Connecte au broker MQTT")

            await client.subscribe([('buzzer/config', 1)])
            print("Abonne au topic 'buzzer/config'")

            await client.subscribe([('buzzer/control', 1)])
            print("Abonne au topic 'buzzer/control'")

            await client.subscribe([('buzzer/pressed', 1)])
            print("Abonne au topic 'buzzer/pressed'")

            while True:
                message = await client.deliver_message()
                packet = message.publish_packet
                handle_message(packet.payload.data, packet.variable_header.topic_name)

        except asyncio.CancelledError:
            await client.disconnect()
            print("Deconnexion MQTT propre")
            break

        except Exception as e:
            print(f"Erreur MQTT : {e}, nouvelle tentative dans 5 secondes...")
            await asyncio.sleep(5)


if __name__ == '__main__':
    config = lire_config()
    print("Config actuelle :", config)

    loop = asyncio.get_event_loop()

    # demarrage MQTT en tache de fond
    task = loop.create_task(mqtt_client())

    # instancie le controleur (garde la meme loop pour run_coroutine_threadsafe)
    buzzer_count = 8
    controller = ButtonController(buzzer_count, loop)

    try:
        # lance la boucle asyncio (necessaire !)
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # arret propre
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
        print("Programme arrete proprement")
