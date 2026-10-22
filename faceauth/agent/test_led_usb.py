
import serial
import time

PORT_ESP32 = "COM5"

esp32 = serial.Serial(
    PORT_ESP32,
    115200,
    timeout=2
)

# Attendre le redemarrage de l'ESP32
time.sleep(2)

print("LED verte pendant 5 secondes")
esp32.write(b"RECONNU\n")
time.sleep(5)

print("LED rouge pendant 5 secondes")
esp32.write(b"NON_RECONNU\n")
time.sleep(5)

print("Extinction des LED")
esp32.write(b"OFF\n")

esp32.close()
print("Test termine")
