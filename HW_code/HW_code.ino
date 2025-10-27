/*
 * ESP32-S3 + SprintIR-6S(5%) 20Hz 리더 (듀얼 코어 및 이중 버퍼)
 *
 * 최적화 로직:
 * 1. 듀얼 코어 활용:
 * - Core 1 (Arduino loop): 센서 UART 수신 전용 (실시간 처리)
 * - Core 0 (sendingTask): WiFi 및 HTTP 전송 전용 (블로킹 작업)
 * 2. 이중 버퍼 (Ping-Pong Buffer):
 * - 두 코어가 충돌 없이(Race condition 방지) 데이터를 주고받기 위해
 * 2개의 버퍼(A, B)를 번갈아 사용.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>

enum LedState {
    INIT,             // (Red) 부팅 및 초기화 중
    WIFI_CONNECTING,  // (Pulsing Yellow) WiFi 연결 시도 중
    WAITING,          // (Dim White) 대기 중 (센서 데이터 기다림)
    RECEIVING,        // (Green) UART 데이터 수신 중
    SENDING,          // (Pulsing Blue) Core 0가 서버로 데이터 전송 중
    ERROR             // (Pulsing Red) 배치 드롭 등 오류 발생
};

class StatusLED {
private:
    LedState currentState = INIT;
    LedState lastState = INIT;
    unsigned long lastBlink = 0;
    bool blinkOn = false;

public:
    void update() {
        unsigned long now = millis();

        if (currentState != lastState) {
            lastBlink = now;
            blinkOn = true;
            lastState = currentState;
        }

        switch (currentState) {
            case INIT:
                rgbLedWrite(RGB_BUILTIN, 255, 0, 0); // Solid Red
                break;
            case WIFI_CONNECTING:
                if (now - lastBlink > 500) {
                    lastBlink = now;
                    blinkOn = !blinkOn;
                    if (blinkOn) rgbLedWrite(RGB_BUILTIN, 128, 128, 0); // Yellow
                    else rgbLedWrite(RGB_BUILTIN, 0, 0, 0); // Off
                }
                break;
            case WAITING:
                rgbLedWrite(RGB_BUILTIN, 20, 20, 20); // Solid Dim White
                break;
            case RECEIVING:
                rgbLedWrite(RGB_BUILTIN, 0, 255, 0); // Solid Green
                break;
            case SENDING:
                if (now - lastBlink > 250) {
                    lastBlink = now;
                    blinkOn = !blinkOn;
                    if (blinkOn) rgbLedWrite(RGB_BUILTIN, 0, 0, 255); // Blue
                    else rgbLedWrite(RGB_BUILTIN, 0, 0, 0); // Off
                }
                break;
            case ERROR:
                if (now - lastBlink > 250) {
                    lastBlink = now;
                    blinkOn = !blinkOn;
                    if (blinkOn) rgbLedWrite(RGB_BUILTIN, 255, 0, 0); // Bright Red
                    else rgbLedWrite(RGB_BUILTIN, 0, 0, 0); // Off
                }
                break;
        }
    }

    void setState(LedState newState) {
        currentState = newState;
    }
    
    LedState getState() {
        return currentState;
    }
};

StatusLED status;

// ====== 핀 정의 ======
static const uint8_t SENSOR_RX_PIN = 16; // ESP32-S3 RX (센서 TX_Out 연결)
static const uint8_t SENSOR_TX_PIN = 17; // ESP32-S3 TX (센서 RX_In  연결)

// ====== WiFi 및 서버 설정 ======
const char *ssid = ""; // WiFi name
const char *password = ""; // WiFi Password
const char *serverUrl = "http://{server_IP}:{server_port}/co2_data";

// ====== UART 인스턴스 ======
HardwareSerial CO2(1);

// ====== 상태 ======
String lineBuf;


// ====== 이중 버퍼 설정 ======
const size_t BATCH_SIZE = 10;
String z_buffer_A[BATCH_SIZE]; //16로 변경 (서버의 '<20i'와 일치)
String z_buffer_B[BATCH_SIZE];

String *active_buffer = z_buffer_A;
String *send_buffer = nullptr;
volatile size_t active_buffer_index = 0;

volatile bool isSendingData = false;
volatile bool batchDropped = false;
volatile bool buffer_ready = false; // 새로운 플래그

TaskHandle_t sendingTaskHandle = NULL;

// ====== 평균 버퍼 ======
const size_t AVG_WINDOW = 4;
int16_t avg_window[AVG_WINDOW];
size_t avg_index = 0;

void handleLine(const String &ln){
    String s = ln;
    s.trim();
    if (s.length() <= 1) return;

    if (s[0] == 'Z' || s[0] == 'z')
    {
        int sp = s.indexOf(' ');
        String valStr = (sp >= 0) ? s.substring(sp + 1) : s.substring(1);
        valStr.trim();
        
        int16_t z = valStr.toInt();
        
        // 평균 윈도우에 추가
        avg_window[avg_index] = z;
        avg_index++;
        
        // 4개 모이면 평균 계산
        if (avg_index >= AVG_WINDOW)
        {
            int32_t sum = 0;
            for (int i = 0; i < AVG_WINDOW; i++) {
                sum += avg_window[i];
            }
            int16_t avg = sum / AVG_WINDOW;
            
            // 평균값을 문자열로 저장
            String avgStr = "Z " + String(avg);
            
            if (active_buffer_index < BATCH_SIZE)
            {
                active_buffer[active_buffer_index] = avgStr;
                active_buffer_index++;
            }

            if (active_buffer_index >= BATCH_SIZE)
            {
                if (!buffer_ready)
                {
                    send_buffer = active_buffer;
                    active_buffer = (active_buffer == z_buffer_A) ? z_buffer_B : z_buffer_A;
                    buffer_ready = true;
                    Serial.printf("[BATCH] Avg: %d\n", avg);
                }
                else
                {
                    Serial.println("[WARN] Dropping batch!");
                    batchDropped = true;
                }
                
                active_buffer_index = 0;
            }
            
            avg_index = 0; // 윈도우 리셋
        }
    }
}

// ====== 센서 명령 ======
void sendCmd(const char *s)
{
    CO2.print(s);
    CO2.print("\r\n");
}

void forceStreamingMode() { 
    sendCmd("K 1"); // 20Hz 스트리밍 모드
}

void kickStart() { 
    CO2.write('\n'); 
}

// ====== WiFi 연결 ======
void setupWiFi(){
    delay(10);
    Serial.println("\n[WIFI] Connecting to " + String(ssid));
    WiFi.begin(ssid, password);

    unsigned long wifiStartTime = millis();
    while (WiFi.status() != WL_CONNECTED)
    {
        status.setState(WIFI_CONNECTING);
        status.update();
        delay(500);
        Serial.print(".");
        if (millis() - wifiStartTime > 20000)
        {
            Serial.println("\n[WIFI] Failed to connect. Restarting...");
            ESP.restart();
        }
    }
    Serial.println("\n[WIFI] Connected! IP address: " + WiFi.localIP().toString());
}

// ====== 데이터 전송 (Core 0) ======
void sendBatchData(String *buffer_to_send){
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[HTTP] WiFi disconnected. Skipping send.");
        return;
    }
    
    isSendingData = true;
    
    // 20개 문자열을 줄바꿈으로 연결
    String payload = "";
    for (int i = 0; i < BATCH_SIZE; i++) {
        payload += buffer_to_send[i] + "\n";
    }

    HTTPClient http;
    http.begin(serverUrl);
    http.addHeader("Content-Type", "text/plain");
    http.setTimeout(5000);

    Serial.printf("[HTTP-C0] Sending batch (%u bytes)...\n", payload.length());

    int httpCode = http.POST(payload);

    if (httpCode > 0) {
        Serial.printf("[HTTP-C0] Send complete, code: %d\n", httpCode);
    } else {
        Serial.printf("[HTTP-C0] Failed: %s\n", http.errorToString(httpCode).c_str());
    }

    http.end();
    isSendingData = false;
}

// ====== 전송 태스크 ======
void sendingTask(void *pvParameters){
    Serial.println("[TASK-C0] Sending Task started.");
    
    while (true)
    {
        if (buffer_ready)
        {
            String *buffer_to_send = send_buffer;
            buffer_ready = false; // 플래그 해제
            
            sendBatchData(buffer_to_send);
        }
        delay(10);
    }
}
// ====== SETUP (Core 1) ======
void setup(){
    status.setState(INIT);
    status.update();

    Serial.begin(115200);
    unsigned long t0 = millis();
    while (!Serial && millis() - t0 < 2000) {}

    Serial.println("\n========================================");
    Serial.println("ESP32-S3 + SprintIR-6S CO2 Sensor");
    Serial.println("Dual-Core with Ping-Pong Buffer");
    Serial.println("========================================\n");

    // WiFi 연결
    setupWiFi();

    // Core 0 전송 태스크 시작
    xTaskCreatePinnedToCore(
        sendingTask,
        "SendingTask",
        8192,  // 스택 크기 증가 (HTTPClient 안정성)
        NULL,
        1,
        &sendingTaskHandle,
        0
    );

    // 센서 UART 시작 (38400 baud for 20Hz mode)
    CO2.begin(9600, SERIAL_8N1, SENSOR_RX_PIN, SENSOR_TX_PIN);
    delay(500);

    Serial.println("[CO2-C1] SprintIR-6S initialized (UART 38400 8N1)");
    delay(100);

    // 센서 정보 요청
    sendCmd("Y");
    delay(200);

    // 20Hz 스트리밍 모드 설정
    forceStreamingMode();
    delay(100);
    
    // 스트림 시작
    kickStart();
    delay(100);

    Serial.println("[MAIN-C1] Ready. Core 1(Sensing) and Core 0(Sending) running.");
    Serial.printf("[CONFIG] Batch size: %d, Payload: %d bytes\n", 
                  BATCH_SIZE, BATCH_SIZE * 4);
    
    status.setState(WAITING);
}

// ====== LOOP (Core 1) ======
void loop(){

    static unsigned long lastDebug = 0;
    if (millis() - lastDebug > 2000) {
        lastDebug = millis();
        Serial.printf("[DEBUG] UART available: %d bytes\n", CO2.available());
        
        // 수동으로 Z 요청
        sendCmd("Z");
    }

    
    // LED 상태 결정
    if (isSendingData) {
        status.setState(SENDING);
    } else if (batchDropped) {
        status.setState(ERROR);
        delay(1000); 
        batchDropped = false;
        status.setState(WAITING);
    } else if (CO2.available()) {
        status.setState(RECEIVING);
    } else {
        status.setState(WAITING);
    }
    
    status.update();

    // UART 데이터 수신
    while (CO2.available()) {
        char c = (char)CO2.read();
        if (c == '\r')
            continue;
        if (c == '\n') {
            if (lineBuf.length() > 0) {
                handleLine(lineBuf);
                lineBuf = "";
            }
        }
        else {
            lineBuf += c;
            if (lineBuf.length() > 128)  // 버퍼 크기 증가
                lineBuf.remove(0, lineBuf.length() - 128);
        }
    }
    
    delay(1);
}
