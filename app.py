import configparser
import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt
import pytz

# ================= 读取配置文件 =================
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.conf')
config = configparser.ConfigParser()
config.read(CONFIG_FILE, encoding='utf-8')

WEB_HOST = config.get('web', 'host', fallback='0.0.0.0')
WEB_PORT = config.getint('web', 'port', fallback=18080)

MQTT_BROKER = config.get('mqtt', 'broker', fallback='127.0.0.1')
MQTT_PORT = config.getint('mqtt', 'port', fallback=1883)
MQTT_TOPIC = config.get('mqtt', 'topic', fallback='device/zm1/#')
MQTT_USER = config.get('mqtt', 'user', fallback='') or None
MQTT_PASSWORD = config.get('mqtt', 'password', fallback='') or None

DB_FILE = config.get('storage', 'db_file', fallback='sensor_history.db')
INTERVAL_SECONDS = config.getint('storage', 'interval_seconds', fallback=600)
RETENTION_DAYS = config.getint('storage', 'retention_days', fallback=7)

OUTDOOR_ENABLED = config.getboolean('outdoor', 'enabled', fallback=True)
OUTDOOR_TOKEN = config.get('outdoor', 'token', fallback='demo')
OUTDOOR_CITY = config.get('outdoor', 'city', fallback='shanghai')
OUTDOOR_INTERVAL = config.getint('outdoor', 'refresh_interval', fallback=1800)

tz_bj = pytz.timezone('Asia/Shanghai')
app = Flask(__name__)

device_cache = {}
last_saved_time = {}
cache_lock = threading.Lock()

outdoor_cache = {
    "city": OUTDOOR_CITY,
    "pm25": None, "pm10": None, "o3": None,
    "so2": None, "no2": None, "aqi": None, "aqi_cn": None,
    "updated_at": None, "status": "等待获取..."
}

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT NOT NULL, temperature REAL, humidity REAL,
                pm25 REAL, formaldehyde REAL, created_at DATETIME NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mac_time ON sensor_history(mac, created_at);")
        conn.execute("CREATE TABLE IF NOT EXISTS device_info (mac TEXT PRIMARY KEY, name TEXT);")
        conn.commit()

def load_device_info():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT mac, name FROM device_info").fetchall()
            with cache_lock:
                for row in rows:
                    mac = row['mac']
                    device_cache[mac] = {
                        "mac": mac, "name": row['name'], "temperature": None,
                        "humidity": None, "pm25": None, "formaldehyde": None,
                        "updated_at": None
                    }
    except Exception as e:
        pass

def save_record_and_cleanup(mac, data, now_str):
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO sensor_history (mac, temperature, humidity, pm25, formaldehyde, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (mac, data.get("temperature"), data.get("humidity"), data.get("pm25"), data.get("formaldehyde"), now_str))

            cutoff_date = (datetime.now(tz_bj) - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("DELETE FROM sensor_history WHERE created_at < ?", (cutoff_date,))
            conn.commit()
    except Exception as e:
        pass

# US AQI to Concentration
def aqi_to_pm25_conc(aqi):
    if aqi is None: return None
    if aqi <= 50: return round((12.0 - 0.0) / (50 - 0) * aqi, 1)
    elif aqi <= 100: return round((35.4 - 12.1) / (100 - 51) * (aqi - 51) + 12.1, 1)
    elif aqi <= 150: return round((55.4 - 35.5) / (150 - 101) * (aqi - 101) + 35.5, 1)
    elif aqi <= 200: return round((150.4 - 55.5) / (200 - 151) * (aqi - 151) + 55.5, 1)
    elif aqi <= 300: return round((250.4 - 150.5) / (300 - 201) * (aqi - 201) + 150.5, 1)
    elif aqi <= 400: return round((350.4 - 250.5) / (400 - 301) * (aqi - 301) + 250.5, 1)
    else: return round((500.4 - 350.5) / (500 - 401) * (aqi - 401) + 350.5, 1)

def aqi_to_pm10_conc(aqi):
    if aqi is None: return None
    if aqi <= 50: return round((54 - 0) / (50 - 0) * aqi)
    elif aqi <= 100: return round((154 - 55) / (100 - 51) * (aqi - 51) + 55)
    elif aqi <= 150: return round((254 - 155) / (150 - 101) * (aqi - 101) + 155)
    elif aqi <= 200: return round((354 - 255) / (200 - 151) * (aqi - 151) + 255)
    elif aqi <= 300: return round((424 - 355) / (300 - 201) * (aqi - 201) + 355)
    else: return round((604 - 425) / (400 - 301) * (aqi - 301) + 425)

# Concentration to CN AQI (HJ 633-2012)
def pm25_conc_to_aqi_cn(c):
    if c is None: return None
    if c <= 35: return round(50 / 35 * c)
    elif c <= 75: return round(50 / 40 * (c - 35) + 50)
    elif c <= 115: return round(50 / 40 * (c - 75) + 100)
    elif c <= 150: return round(50 / 35 * (c - 115) + 150)
    elif c <= 250: return round(100 / 100 * (c - 150) + 200)
    elif c <= 350: return round(100 / 100 * (c - 250) + 300)
    else: return round(100 / 150 * (c - 350) + 400)

def pm10_conc_to_aqi_cn(c):
    if c is None: return None
    if c <= 50: return round(50 / 50 * c)
    elif c <= 150: return round(50 / 100 * (c - 50) + 50)
    elif c <= 250: return round(50 / 100 * (c - 150) + 100)
    elif c <= 350: return round(50 / 100 * (c - 250) + 150)
    elif c <= 420: return round(100 / 70 * (c - 350) + 200)
    elif c <= 500: return round(100 / 80 * (c - 420) + 300)
    else: return round(100 / 100 * (c - 500) + 400)

def fetch_outdoor_task():
    url = f"https://api.waqi.info/feed/{OUTDOOR_CITY}/?token={OUTDOOR_TOKEN}"
    while True:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_json = json.loads(resp.read().decode('utf-8'))
                if res_json.get("status") == "ok":
                    data = res_json.get("data", {})
                    iaqi = data.get("iaqi", {})

                    overall_aqi = data.get("aqi")
                    pm25_conc = aqi_to_pm25_conc((iaqi.get("pm25") or {}).get("v"))
                    pm10_conc = aqi_to_pm10_conc((iaqi.get("pm10") or {}).get("v"))
                    
                    cn_aqis = [x for x in [pm25_conc_to_aqi_cn(pm25_conc), pm10_conc_to_aqi_cn(pm10_conc)] if x is not None]
                    overall_aqi_cn = max(cn_aqis) if cn_aqis else None

                    # 根据 US AQI 判定等级 (美国 EPA 标准) - 这与“在意空气”一致
                    status_desc = "未知"
                    if overall_aqi is not None:
                        if overall_aqi > 300: status_desc = "严重污染"
                        elif overall_aqi > 200: status_desc = "非常不健康"
                        elif overall_aqi > 150: status_desc = "不健康"
                        elif overall_aqi > 100: status_desc = "对敏感人群不健康"
                        elif overall_aqi > 50: status_desc = "中等"
                        else: status_desc = "优"

                    outdoor_cache.update({
                        "pm25": pm25_conc,
                        "pm10": pm10_conc,
                        "o3": (iaqi.get("o3") or {}).get("v"),
                        "so2": (iaqi.get("so2") or {}).get("v"),
                        "no2": (iaqi.get("no2") or {}).get("v"),
                        "aqi": overall_aqi,
                        "aqi_cn": overall_aqi_cn,
                        "status": status_desc,
                        "updated_at": datetime.now(tz_bj).strftime('%Y-%m-%d %H:%M:%S')
                    })
        except Exception:
            pass
        time.sleep(OUTDOOR_INTERVAL)

def on_connect(client, userdata, flags, rc, *args):
    client.subscribe(MQTT_TOPIC)

def on_message(client, userdata, msg):
    try:
        topic_parts = msg.topic.split('/')
        if len(topic_parts) < 4: return

        mac = topic_parts[2]
        msg_type = topic_parts[3]
        payload = json.loads(msg.payload.decode('utf-8'))
        now_str = datetime.now(tz_bj).strftime('%Y-%m-%d %H:%M:%S')

        with cache_lock:
            if mac not in device_cache:
                device_cache[mac] = {
                    "mac": mac, "name": f"设备_{mac[-4:]}", "temperature": None,
                    "humidity": None, "pm25": None, "formaldehyde": None,
                    "updated_at": now_str
                }

            if msg_type == 'state' and "name" in payload:
                new_name = payload["name"]
                if device_cache[mac].get("name") != new_name:
                    device_cache[mac]["name"] = new_name
                    with get_db() as conn:
                        conn.execute("INSERT OR REPLACE INTO device_info (mac, name) VALUES (?, ?)", (mac, new_name))
                        conn.commit()

            elif msg_type == 'sensor':
                device_cache[mac]["temperature"] = float(payload.get("temperature", 0))
                device_cache[mac]["humidity"] = float(payload.get("humidity", 0))
                device_cache[mac]["pm25"] = float(payload.get("PM25", 0))
                device_cache[mac]["formaldehyde"] = float(payload.get("formaldehyde", 0))
                device_cache[mac]["updated_at"] = now_str

                current_timestamp = time.time()
                if current_timestamp - last_saved_time.get(mac, 0) >= INTERVAL_SECONDS:
                    last_saved_time[mac] = current_timestamp
                    save_record_and_cleanup(mac, device_cache[mac], now_str)
    except Exception:
        pass

def start_mqtt():
    try: client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError: client = mqtt.Client()
    if MQTT_USER and MQTT_PASSWORD: client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            client.loop_forever()
        except Exception:
            time.sleep(5)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/devices/latest', methods=['GET'])
def get_latest_devices():
    with cache_lock: return jsonify(list(device_cache.values()))

@app.route('/api/devices/<mac>/history', methods=['GET'])
def get_device_history(mac):
    time_range = request.args.get('range', '24h')
    start_time = (datetime.now(tz_bj) - timedelta(days=7 if time_range == '7d' else 1)).strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        rows = conn.execute("SELECT temperature, humidity, pm25, formaldehyde, created_at FROM sensor_history WHERE mac = ? AND created_at >= ? ORDER BY created_at ASC", (mac, start_time)).fetchall()
    history = [{"time": r["created_at"], "temperature": r["temperature"], "humidity": r["humidity"], "pm25": r["pm25"], "formaldehyde": r["formaldehyde"]} for r in rows]
    return jsonify({"mac": mac, "range": time_range, "data": history})

@app.route('/api/outdoor', methods=['GET'])
def get_outdoor(): return jsonify(outdoor_cache)

if __name__ == '__main__':
    init_db()
    load_device_info()
    threading.Thread(target=start_mqtt, daemon=True).start()
    if OUTDOOR_ENABLED: threading.Thread(target=fetch_outdoor_task, daemon=True).start()
    from waitress import serve
    print(f"服务已启动: http://{WEB_HOST}:{WEB_PORT}")
    serve(app, host=WEB_HOST, port=WEB_PORT)
