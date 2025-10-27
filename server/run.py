import os
import asyncio
import json
from datetime import datetime
from typing import List

import uvicorn
import numpy as np
from fastapi import (
    FastAPI, UploadFile, File, HTTPException, Request,
    WebSocket, WebSocketDisconnect
)
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware  # CORS 추가

app = FastAPI(title="ESP32 Data Stream Server")

# CORS 설정 (다른 포트/도메인에서 접근 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NPY_SAVE_DIR = "./co2_data_recordings"
if not os.path.exists(NPY_SAVE_DIR):
    os.makedirs(NPY_SAVE_DIR)

UPLOAD_DIR = "./uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

BATCH_SIZE = 10
EXPECTED_PAYLOAD_SIZE = BATCH_SIZE * 4
STRUCT_FORMAT = f'<{BATCH_SIZE}i'  # Little-endian, 32-bit signed integers

data_stream_buffer = []
last_data_time = None
is_receiving = False
timeout_task = None
STREAM_TIMEOUT_SEC = 5.0
lock = asyncio.Lock()

# 통계 정보 추가
total_batches_received = 0
total_points_received = 0

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"[WS] Client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"[WS] Client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast_data(self, data: List[int]):
        message = json.dumps(data)
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"[WS] Error broadcasting to client: {e}")
                disconnected.append(connection)
        
        # 연결이 끊긴 클라이언트 제거
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()

async def check_stream_timeout():
    global is_receiving, data_stream_buffer, last_data_time
    
    while is_receiving:
        await asyncio.sleep(1.0)
        
        async with lock:
            if last_data_time is None:
                continue
            elapsed = (datetime.now() - last_data_time).total_seconds()
            
            if elapsed > STREAM_TIMEOUT_SEC:
                print(f"[SESSION] Timeout: {elapsed:.1f}s. Data stream stopped.")
                save_buffer_to_npy()
                data_stream_buffer = []
                is_receiving = False
                last_data_time = None
                break

def save_buffer_to_npy():
    global data_stream_buffer
    if not data_stream_buffer:
        print("[NPY] No data to save.")
        return
    try:
        data_array = np.array(data_stream_buffer, dtype=np.int32)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"co2_data_{timestamp_str}.npy"
        filepath = os.path.join(NPY_SAVE_DIR, filename)
        np.save(filepath, data_array)
        
        # 통계 정보 출력
        avg_val = np.mean(data_array)
        min_val = np.min(data_array)
        max_val = np.max(data_array)
        print(f"[NPY] Saved {filepath}")
        print(f"      Total points: {len(data_stream_buffer)}")
        print(f"      Avg: {avg_val:.1f}, Min: {min_val}, Max: {max_val}")
    except Exception as e:
        print(f"[NPY] Error saving .npy file: {e}")

@app.post("/co2_data")
async def receive_co2_raw_data(request: Request):
    global is_receiving, data_stream_buffer, last_data_time, timeout_task
    global total_batches_received, total_points_received

    body_text = await request.body()
    body_str = body_text.decode('utf-8')
    
    lines = body_str.strip().split('\n')
    
    raw_z_values_list = []
    for line in lines:
        line = line.strip()
        if not line or not (line.startswith('Z') or line.startswith('z')):
            continue
        
        # "Z 00421" → 421 추출
        parts = line.split()
        if len(parts) >= 2:
            try:
                value = int(parts[1])
                raw_z_values_list.append(value)
            except ValueError:
                print(f"[WARN] Failed to parse: {line}")
    
    if not raw_z_values_list:
        raise HTTPException(status_code=400, detail="No valid data")
    
    async with lock:
        if not is_receiving:
            print("[SESSION] New data stream started.")
            is_receiving = True
            data_stream_buffer = raw_z_values_list
            total_batches_received = 0
            total_points_received = 0
            timeout_task = asyncio.create_task(check_stream_timeout())
        else:
            data_stream_buffer.extend(raw_z_values_list)
        
        last_data_time = datetime.now()
        total_batches_received += 1
        total_points_received += len(raw_z_values_list)
    
    if manager.active_connections:
        asyncio.create_task(manager.broadcast_data(raw_z_values_list))

    if total_batches_received % 10 == 0:
        avg_val = np.mean(data_stream_buffer)
        print(f"[DATA] Batches: {total_batches_received}, "
              f"Total points: {total_points_received}, "
              f"Avg: {avg_val:.1f}")
    
    return {"status": "success", "received_count": len(raw_z_values_list)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"[WS] Error: {e}")
        manager.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
async def get_monitoring_page():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>CO2 실시간 모니터링</title>
        <meta charset="UTF-8">
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; 
                margin: 0; padding: 20px; background-color: #f4f7f6; 
            }
            h1 { color: #333; margin-bottom: 10px; }
            #status { 
                margin: 10px 0; font-weight: bold; padding: 10px;
                border-radius: 4px; background-color: #fff;
            }
            #stats {
                margin: 10px 0; padding: 10px;
                background-color: #fff; border-radius: 4px;
                display: flex; gap: 20px; flex-wrap: wrap;
            }
            .stat-item {
                padding: 5px 10px;
                border-left: 3px solid #4bc0c0;
            }
            .stat-label { font-size: 12px; color: #666; }
            .stat-value { font-size: 20px; font-weight: bold; color: #333; }
            .chart-container { 
                width: 90vw; max-width: 1200px; margin: 20px auto; 
                background-color: #fff; border-radius: 8px; 
                box-shadow: 0 4px 12px rgba(0,0,0,0.05); padding: 20px;
            }
            .controls {
                margin: 20px 0; padding: 10px;
                background-color: #fff; border-radius: 4px;
            }
            button {
                padding: 8px 16px; margin: 5px;
                border: none; border-radius: 4px;
                background-color: #4bc0c0; color: white;
                cursor: pointer; font-size: 14px;
            }
            button:hover { background-color: #3aa0a0; }
            button:disabled { background-color: #ccc; cursor: not-allowed; }
        </style>
    </head>
    <body>
        <h1>🌫️ CO2 센서 실시간 모니터링 (SprintIR-6S)</h1>
        <div id="status">연결 중...</div>
        <div id="stats">
            <div class="stat-item">
                <div class="stat-label">현재값</div>
                <div class="stat-value" id="current">-</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">평균</div>
                <div class="stat-value" id="average">-</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">최소</div>
                <div class="stat-value" id="min">-</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">최대</div>
                <div class="stat-value" id="max">-</div>
            </div>
            <div class="stat-item">
                <div class="stat-label">수신 포인트</div>
                <div class="stat-value" id="points">0</div>
            </div>
        </div>
        <div class="controls">
            <button onclick="resetChart()">차트 초기화</button>
            <button onclick="downloadData()">데이터 다운로드 (CSV)</button>
        </div>
        <div class="chart-container">
            <canvas id="co2Chart"></canvas>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
        <script>
            const statusEl = document.getElementById('status');
            const ctx = document.getElementById('co2Chart').getContext('2d');
            
            const chartData = {
                labels: [],
                datasets: [{
                    label: 'Raw "Z" Value (ppm)',
                    data: [],
                    borderColor: 'rgb(75, 192, 192)',
                    backgroundColor: 'rgba(75, 192, 192, 0.1)',
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.1
                }]
            };

            const co2Chart = new Chart(ctx, {
                type: 'line',
                data: chartData,
                options: {
                    animation: false,
                    responsive: true,
                    maintainAspectRatio: true,
                    scales: {
                        x: {
                            type: 'linear',
                            title: { display: true, text: 'Data Point Index' }
                        },
                        y: {
                            beginAtZero: false,
                            title: { display: true, text: 'Raw "Z" Value (ppm)' }
                        }
                    },
                    plugins: {
                        legend: { display: true },
                        tooltip: { enabled: true }
                    }
                }
            });

            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsURL = `${wsProtocol}//${window.location.host}/ws`;
            const ws = new WebSocket(wsURL);
            let pointCounter = 0;
            const MAX_POINTS_TO_SHOW = 2000;
            let allData = [];

            ws.onopen = () => {
                console.log('WebSocket connected.');
                statusEl.textContent = '✅ 서버에 연결됨 (데이터 수신 대기 중)';
                statusEl.style.color = 'green';
                statusEl.style.backgroundColor = '#d4edda';
            };

            ws.onmessage = (event) => {
                const batchData = JSON.parse(event.data);
                
                batchData.forEach(value => {
                    chartData.labels.push(pointCounter++);
                    chartData.datasets[0].data.push(value);
                    allData.push(value);

                    if (chartData.labels.length > MAX_POINTS_TO_SHOW) {
                        chartData.labels.shift();
                        chartData.datasets[0].data.shift();
                    }
                });

                co2Chart.update('none'); // 애니메이션 없이 업데이트
                
                // 통계 업데이트
                updateStats();
                
                if (statusEl.textContent !== '📡 데이터 수신 중...') {
                    statusEl.textContent = '📡 데이터 수신 중...';
                    statusEl.style.backgroundColor = '#d1ecf1';
                }
            };

            ws.onclose = () => {
                console.log('WebSocket disconnected.');
                statusEl.textContent = '❌ 서버 연결 끊어짐';
                statusEl.style.color = 'red';
                statusEl.style.backgroundColor = '#f8d7da';
            };

            ws.onerror = (error) => {
                console.error('WebSocket Error: ', error);
                statusEl.textContent = '⚠️ 연결 오류 발생';
                statusEl.style.color = 'red';
                statusEl.style.backgroundColor = '#f8d7da';
            };

            function updateStats() {
                const currentData = chartData.datasets[0].data;
                if (currentData.length === 0) return;

                const current = currentData[currentData.length - 1];
                const sum = currentData.reduce((a, b) => a + b, 0);
                const avg = sum / currentData.length;
                const min = Math.min(...currentData);
                const max = Math.max(...currentData);

                document.getElementById('current').textContent = current;
                document.getElementById('average').textContent = avg.toFixed(1);
                document.getElementById('min').textContent = min;
                document.getElementById('max').textContent = max;
                document.getElementById('points').textContent = allData.length;
            }

            function resetChart() {
                if (confirm('차트와 데이터를 초기화하시겠습니까?')) {
                    chartData.labels = [];
                    chartData.datasets[0].data = [];
                    allData = [];
                    pointCounter = 0;
                    co2Chart.update();
                    updateStats();
                }
            }

            function downloadData() {
                if (allData.length === 0) {
                    alert('다운로드할 데이터가 없습니다.');
                    return;
                }

                let csv = 'Index,Value\\n';
                allData.forEach((value, index) => {
                    csv += `${index},${value}\\n`;
                });

                const blob = new Blob([csv], { type: 'text/csv' });
                const url = URL.createObjectURL(blob);
const a = document.createElement('a');
                a.href = url;
                a.download = `co2_data_${new Date().toISOString().replace(/[:.]/g, '-')}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/upload_and_execute")
async def upload_and_run_file(file: UploadFile = File(...)):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        print(f"[FILE] File '{file.filename}' saved to {file_path}")
    except Exception as e:
        print(f"[FILE] Error saving file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    return {
        "status": "file_saved", 
        "filename": file.filename, 
        "message": "File saved but NOT executed for security reasons."
    }

@app.get("/stats")
async def get_stats():
    """현재 세션 통계 반환"""
    async with lock:
        if not data_stream_buffer:
            return {"status": "no_data"}
        
        data_array = np.array(data_stream_buffer, dtype=np.int32)
        return {
            "status": "active" if is_receiving else "ended",
            "total_points": len(data_stream_buffer),
            "total_batches": total_batches_received,
            "average": float(np.mean(data_array)),
            "min": int(np.min(data_array)),
            "max": int(np.max(data_array)),
            "std": float(np.std(data_array)),
            "last_update": last_data_time.isoformat() if last_data_time else None
        }

if __name__ == "__main__":
    ip = "" # Enter your IP as str
    port = int() # Enter your Port as int
    print("=" * 60)
    print("🚀 FastAPI CO2 Data Server Starting...")
    print("=" * 60)
    print(f"📡 Server URL: http://{ip}:{port}")
    print(f"🌐 Real-time plot: http://127.0.0.1:{port}/")
    print(f"📊 Stats endpoint: http://127.0.0.1:{port}/stats")
    print(f"💾 Data save directory: {NPY_SAVE_DIR}")
    print(f"⏱️  Stream timeout: {STREAM_TIMEOUT_SEC}s")
    print(f"📦 Batch size: {BATCH_SIZE} points")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")