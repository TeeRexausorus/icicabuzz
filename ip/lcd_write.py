#!/usr/bin/env python3
import sys
from RPLCD.i2c import CharLCD

# Adapte selon ton écran
I2C_ADDR = 0x27   # ou 0x3F
I2C_PORT = 1
LCD_COLS = 16
LCD_ROWS = 2

def fit(text, width):
    return str(text)[:width].ljust(width)

def main():
    line1 = sys.argv[1] if len(sys.argv) > 1 else ""
    line2 = sys.argv[2] if len(sys.argv) > 2 else ""

    lcd = CharLCD(
        i2c_expander='PCF8574',
        address=I2C_ADDR,
        port=I2C_PORT,
        cols=LCD_COLS,
        rows=LCD_ROWS,
        charmap='A02',
        auto_linebreaks=False,
        backlight_enabled=True,
    )

    lcd.clear()
    lcd.cursor_pos = (0, 0)
    lcd.write_string(fit(line1, LCD_COLS))
    lcd.cursor_pos = (1, 0)
    lcd.write_string(fit(line2, LCD_COLS))

if __name__ == "__main__":
    main()
