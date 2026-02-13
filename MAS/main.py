import paho.mqtt.client as mqtt
import json
import psycopg2
import time
import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import threading

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
#  COMPRESSOR DATABASE (The "Smart" Lookup)
# ==========================================
COMPRESSOR_PROFILES = {
    "DEMO_MODE": {
        "idle_temp": 26.0,      # < 26C = Resting
        "run_temp": 27.5,       # > 27.5C = Working (Hand Heat)
        "max_temp": 32.0,       # > 32C = Danger
        "idle_vib": 0.5,        # < 0.5g = Still
        "high_speed_vib": 3.0,  # > 3.0g = Level 2 Speed
        "max_vib": 8.0,         # > 8.0g = Loose Mount/Danger
        "loud_mic": 2200        # Sound Threshold
    },
    "R600a_MODERN": {  # Real Factory Profile
        "idle_temp": 35.0,
        "run_temp": 45.0,
        "max_temp": 85.0,
        "idle_vib": 0.1,
        "high_speed_vib": 1.5,
        "max_vib": 3.5,
        "loud_mic": 3000
    }
}

# --- SELECT PROFILE HERE ---
CURRENT_PROFILE = COMPRESSOR_PROFILES["DEMO_MODE"]

# Apply Limits
IDLE_TEMP = CURRENT_PROFILE["idle_temp"]
RUNNING_TEMP = CURRENT_PROFILE["run_temp"]
OVERHEAT_TEMP = CURRENT_PROFILE["max_temp"]
IDLE_VIB = CURRENT_PROFILE["idle_vib"]
HIGH_SPEED_VIB = CURRENT_PROFILE["high_speed_vib"]
MAX_SAFE_VIB = CURRENT_PROFILE["max_vib"]
LOUD_MIC = CURRENT_PROFILE["loud_mic"]

CALIBRATION_WINDOW = 10 
device_brains = {}

def analyze_health(device_id, temp, vib, mic):
    if device_id not in device_brains:
        device_brains[device_id] = {
            'vib_history': [], 'mean': 0.0, 'std': 0.0, 
            'current_level': 'IDLE', 'calibrated': False
        }
    brain = device_brains[device_id]

    # --- PHASE 1: RED ALERT (Safety) ---
    if temp > OVERHEAT_TEMP:
        return {"status": "CRITICAL FAILURE", "message": f"OVERHEAT ({temp}°C)", "fault_score": 10.0}

    # Stall: Hot + Silent + Buzzing
    if temp > RUNNING_TEMP and vib < IDLE_VIB and mic > LOUD_MIC:
        return {"status": "CRITICAL FAILURE", "message": "STALL: Motor Locked (Humming)", "fault_score": 9.5}

    # Stall: Hot + Silent
    if temp > RUNNING_TEMP and vib < IDLE_VIB:
        return {"status": "CRITICAL FAILURE", "message": "STALL: Start Relay Dead", "fault_score": 9.0}

    # Loose Mount: Too Violent
    if vib > MAX_SAFE_VIB:
        return {"status": "CRITICAL FAILURE", "message": "MECHANICAL: Loose Mounting", "fault_score": 8.5}

    # --- PHASE 2: YELLOW ALERT (Performance) ---
    # Gas Leak: Running but Cold
    if vib > IDLE_VIB and temp < IDLE_TEMP and brain['calibrated']:
        return {"status": "WARNING", "message": "GAS LEAK? (Running Cold)", "fault_score": 6.0}

    # Dry Bearing: Running + Loud
    if vib > IDLE_VIB and mic > LOUD_MIC:
        return {"status": "WARNING", "message": "ACOUSTIC FAULT: Grinding Noise", "fault_score": 7.0}

    # --- PHASE 3: GREEN ZONE (Operation) ---
    if vib < IDLE_VIB:
        brain['current_level'] = 'IDLE'
        brain['vib_history'] = [] 
        brain['calibrated'] = False
        return {"status": "STANDBY", "message": "System Idle", "fault_score": 0.0}

    # Adaptive Level Analysis
    brain['vib_history'].append(vib)
    if len(brain['vib_history']) > CALIBRATION_WINDOW:
        brain['vib_history'].pop(0)
    else:
        return {"status": "CALIBRATING", "message": "Analyzing Rhythm...", "fault_score": 0.0}

    current_std = np.std(brain['vib_history'])
    current_mean = np.mean(brain['vib_history'])
    brain['calibrated'] = True

    # Check for Chaos
    if current_std > 1.5:
        return {"status": "AI WARNING", "message": "Unstable/Chaotic Rhythm", "fault_score": 5.0}

    # Determine Level
    new_level = "LOW"
    if current_mean > HIGH_SPEED_VIB: new_level = "HIGH"
    
    previous_level = brain['current_level']
    brain['current_level'] = new_level

    if previous_level != new_level and previous_level != "IDLE":
        return {"status": "OPTIMAL", "message": f"RAMPING UP: {previous_level}->{new_level}", "fault_score": 0.0}
    
    if new_level == "HIGH":
         return {"status": "OPTIMAL (LEVEL 2)", "message": "High Speed Cooling", "fault_score": 0.0}
    else:
         return {"status": "OPTIMAL (LEVEL 1)", "message": "Normal Operation", "fault_score": 0.0}

# --- STANDARD INFRASTRUCTURE ---
time.sleep(5) 
def get_db_connection():
    return psycopg2.connect(dbname="echocold", user="postgres", password="echocold_password", host="timescaledb")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic_parts = msg.topic.split("/")
        if len(topic_parts) < 2: return
        device_id = topic_parts[1]
        
        vib = float(payload.get('vib', 0))
        temp = float(payload.get('temp', 0))
        mic = float(payload.get('mic', 0))
        
        health = analyze_health(device_id, temp, vib, mic)
        print(f"[{device_id}] {health['status']}: {health['message']}")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO sensor_data (device_id, temp, vib, mic) VALUES (%s, %s, %s, %s)", 
                   (device_id, temp, vib, mic))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

def start_mqtt():
    client = mqtt.Client()
    client.on_message = on_message
    client.connect("mosquitto", 443, 60)
    client.subscribe("echocold/+")
    client.loop_forever()

mqtt_thread = threading.Thread(target=start_mqtt)
mqtt_thread.daemon = True
mqtt_thread.start()

@app.get("/history/{device_id}")
def get_history(device_id: str):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT time, temp, vib, mic FROM sensor_data WHERE device_id=%s ORDER BY time DESC LIMIT 50", (device_id,))
        rows = cur.fetchall()
        conn.close()
        data = []
        for row in rows:
            t, v, m = row[1], row[2], row[3]
            health = analyze_health(device_id, t, v, m)
            data.append({
                "time": str(row[0]),
                "temp": t,
                "vib": v,
                "mic": m,
                "status": health['status'],
                "diagnosis": health['message'],
                "fault_score": health['fault_score']
            })
        return data
    except Exception as e:
        return []

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
root@echocold-brain:~#