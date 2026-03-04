#!/usr/bin/env python3
import json
import time
import sys
from signal import SIGINT, SIGTERM, signal
from gpiozero import Button

CONFIG_FILE = 'config.json'
REFRESH_SEC = 0.6   # fréquence d’affichage


def load_pins():
    with open(CONFIG_FILE, 'r') as f:
        cfg = json.load(f)
    pins = cfg.get('input_pins', [])
    if not isinstance(pins, list) or not all(isinstance(p, int) for p in pins):
        raise ValueError("config.json: 'input_pins' doit être une liste d'entiers (numérotation BCM).")
    return pins


def main():
    pins = load_pins()
    if not pins:
        print("Aucune broche dans input_pins. Ajoute-les à config.json.")
        sys.exit(1)

    # Boutons en pull-down (comme ton projet) ; rebond à ~50 ms pour debug réactif
    buttons = [Button(p, pull_up=True, bounce_time=0.05) for p in pins]

    def log_state(idx, pressed):
        print(f"\n{time.strftime('%H:%M:%S')}  GPIO{pins[idx]} -> {'HIGH' if pressed else 'LOW'}", flush=True)

    for i, b in enumerate(buttons):
        b.when_pressed  = (lambda idx=i: log_state(idx, True))
        b.when_released = (lambda idx=i: log_state(idx, False))
    running = True

    def stop_handler(*_):
        nonlocal running
        running = False

    signal(SIGINT,  stop_handler)
    signal(SIGTERM, stop_handler)

    print("Surveillance en cours des entrées:", pins)
    print("Appuie/relâche pour voir les changements. Ctrl+C pour quitter.\n")

    # Affichage “live” compact (ligne qui se réécrit)
    while running:
        snapshot = " | ".join(f"GPIO{p}:{'1' if b.is_pressed else '0'}"
                              for p, b in zip(pins, buttons))
        print("\r" + snapshot + " " * 10, end="", flush=True)
        print("\r\r")
        time.sleep(REFRESH_SEC)

    print("\nArrêt du moniteur.")

if __name__ == '__main__':
    main()

# câblage : GPIOX/R/G/B/3.3V
