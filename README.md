# CO₂ Sensor ESP32 System

## 🧭 Abstract
This project implements a real-time CO₂ sensing and monitoring system using an **ESP32-S3** and a **SprintIR-6S** sensor.  
The ESP32 reads CO₂ concentration via UART and sends data to a Python server over Wi-Fi.  
The server visualizes or stores the readings for further analysis.

---

## 📘 Introduction
Accurate and high-speed CO₂ sensing is important for indoor air quality control and physiological signal monitoring.  
This project provides:
- A lightweight firmware for ESP32-S3 to stream CO₂ data.
- A simple Python server (`run.py`) to receive and display or log the incoming data.
- Easy configuration for network and serial settings.

---

## ⚙️ Method

### 1️⃣ ESP32 Firmware (`device.ino`)
The ESP32 communicates with the SprintIR-6S sensor via UART (default: 9600-8N1)  
and transmits the measured data to a remote server using Wi-Fi (HTTP/Socket).

#### 🧩 User-editable lines
Open the `.ino` file and configure the following lines before uploading:

| Line | Variable | Description |
|------|-----------|-------------|
| **92–93** | `RX_PIN`, `TX_PIN` | UART pin numbers connected to the sensor.<br>Default: `RX_PIN=7`, `TX_PIN=6`.<br>Change according to your wiring. |
| **96–98** | `ssid`, `password`, `serverUrl` | Your Wi-Fi credentials and target server URL.<br>Example:  |
|  |  | ```cpp<br>const char* ssid = "MyWiFi";<br>const char* password = "MyPass";<br>const char* serverUrl = "http://192.168.0.10:5000/upload";<br>``` |

> 💡 **Note:** Ensure that the server is running on the same local network.

#### ⚙️ Upload & Run
1. Select your board: **ESP32-S3 Dev Module** (or equivalent)  
2. Upload the `.ino` file through Arduino IDE or PlatformIO.  
3. Open Serial Monitor at **115200 bps** to confirm:
