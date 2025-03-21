import os
import asyncio
import threading
import sqlite3
import time
import telnetlib3
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template
from flask_cors import CORS

# =============================================================================
# DEFAULT PARAMETERS (Generic Examples)
# =============================================================================
default_params = {
    "thermostat_ip": "192.168.0.0",  # Example generic IP
    "telnet_port": "2000",           # Example port
    "timezone": "America/Los_Angeles",
    "database_dir": r"C:\example\database",
    "templates_dir": r"C:\example\templates",
    # db_file is determined automatically as os.path.join(database_dir, "thermostat_data.db")
    "db_filename": "thermostat_data.db",
    "sensors": json.dumps({"TSTAT_00": "Sensor 1"})  # Default: one sensor
}

# Parameters DB file is stored in the same directory as this script.
PARAM_DB_FILE = os.path.join(os.path.dirname(__file__), "parameters.db")

# =============================================================================
# LOAD/CREATE PARAMETERS FUNCTION
# =============================================================================
def load_parameters():
    """
    Loads configuration parameters from the parameters.db file.
    If the file does not exist, prompts the user for values (except the DB file path,
    which is auto-generated from the database directory and a fixed file name),
    saves them into the parameters database, and returns the parameters.
    """
    try:
        if not os.path.exists(PARAM_DB_FILE):
            print("Parameters file not found. Please enter the following configuration values.")
            thermostat_ip = input(f"Enter Thermostat IP (example: {default_params['thermostat_ip']}): ") or default_params["thermostat_ip"]
            telnet_port = input(f"Enter Telnet Port (example: {default_params['telnet_port']}): ") or default_params["telnet_port"]
            timezone = input(f"Enter Timezone (example: {default_params['timezone']}): ") or default_params["timezone"]
            database_dir = input(f"Enter Database Directory (example: {default_params['database_dir']}): ") or default_params["database_dir"]
            templates_dir = input(f"Enter Templates Directory (example: {default_params['templates_dir']}): ") or default_params["templates_dir"]
            # Automatically determine main DB file path.
            db_filename = default_params["db_filename"]
            db_file = os.path.join(database_dir, db_filename)
    
            # Prompt for number of sensors.
            while True:
                try:
                    sensor_count_input = input("How many sensors are in the system? (1-12): ")
                    sensor_count = int(sensor_count_input)
                    if 1 <= sensor_count <= 12:
                        break
                    else:
                        print("Please enter a number between 1 and 12.")
                except ValueError:
                    print("Please enter a valid number.")
    
            # Prompt for sensor friendly names.
            sensor_map = {}
            for i in range(1, sensor_count+1):
                sensor_id = f"TSTAT_{(i-1):02d}"
                sensor_name = input(f"Enter Sensor {i} Name (example: Sensor {i}): ") or f"Sensor {i}"
                sensor_map[sensor_id] = sensor_name
    
            parameters = {
                "thermostat_ip": thermostat_ip,
                "telnet_port": telnet_port,
                "timezone": timezone,
                "database_dir": database_dir,
                "templates_dir": templates_dir,
                "db_file": db_file,
                "sensors": json.dumps(sensor_map)
            }
            # Create parameters database and table.
            conn = sqlite3.connect(PARAM_DB_FILE)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS parameters (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            for k, v in parameters.items():
                cursor.execute("INSERT INTO parameters (key, value) VALUES (?, ?)", (k, v))
            conn.commit()
            conn.close()
            print("Parameters saved.")
            return parameters
        else:
            # Load parameters from the existing parameters database.
            conn = sqlite3.connect(PARAM_DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM parameters")
            rows = cursor.fetchall()
            conn.close()
            return {key: value for key, value in rows}
    except KeyboardInterrupt:
        print("\nUser cancelled input. Exiting.")
        exit(0)

# Load parameters and override defaults.
params = load_parameters()
TELNET_IP = params.get("thermostat_ip", default_params["thermostat_ip"])
TELNET_PORT = int(params.get("telnet_port", default_params["telnet_port"]))
TZ = params.get("timezone", default_params["timezone"])
DATABASE_DIR = params.get("database_dir", default_params["database_dir"])
TEMPLATES_DIR = params.get("templates_dir", default_params["templates_dir"])
DB_FILE = params.get("db_file", os.path.join(DATABASE_DIR, default_params["db_filename"]))
sensor_map = json.loads(params.get("sensors", default_params["sensors"]))

# =============================================================================
# DEBUG PRINTS (CONFIGURATION)
# =============================================================================
print("Thermostat IP:", TELNET_IP)
print("Telnet Port:", TELNET_PORT)
print("Timezone:", TZ)
print("DATABASE_DIR:", DATABASE_DIR)
print("TEMPLATES_DIR:", TEMPLATES_DIR)
print("DB_FILE:", DB_FILE)
print("Sensor Mapping:", sensor_map)

# =============================================================================
# INITIALIZE FLASK APPLICATION
# =============================================================================
app = Flask(__name__, template_folder=TEMPLATES_DIR)
CORS(app)

# =============================================================================
# DATABASE SETUP AND RETENTION FUNCTIONS
# =============================================================================
def setup_database():
    """
    Create the main SQLite database and the thermostat_logs table if they don't exist.
    Checks if the DB_FILE exists in DATABASE_DIR.
    """
    os.makedirs(DATABASE_DIR, exist_ok=True)
    if not os.path.exists(DB_FILE):
        print(f"Database file not found. Creating new database at {DB_FILE}.")
    else:
        print(f"Using existing database file at {DB_FILE}.")
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS thermostat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                zone TEXT,
                parameter TEXT,
                value INTEGER,
                raw_message TEXT
            )
        ''')
        conn.commit()

def purge_old_data():
    """
    Delete records older than 90 days using local time (TZ) for cutoff.
    """
    cutoff = (datetime.now().astimezone(ZoneInfo(TZ)) - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
    with db_lock:
        try:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM thermostat_logs WHERE timestamp < ?", (cutoff,))
                conn.commit()
                print("Purged old data (older than 90 days)")
        except sqlite3.Error as e:
            print(f"Error purging data: {e}")

def purge_thread_function():
    """
    Runs purge_old_data() every hour in a separate thread.
    """
    while True:
        purge_old_data()
        time.sleep(3600)

# =============================================================================
# GLOBAL LOCK FOR THREAD-SAFE DATABASE OPERATIONS
# =============================================================================
db_lock = threading.Lock()

# =============================================================================
# MESSAGE PARSING AND LOGGING FUNCTIONS
# =============================================================================
last_logged = {}  # Optional duplicate filtering (currently disabled)

def parse_message(message):
    """
    Parse a telnet message and log it to the database.
    
    Expected format: "TSTAT_00 SET_TEMP=0730" (temperature in tenths).
    - Ignores parameters: SET_TEMP_TOLERANCE, SET_HUMIDITY_SP, SET_FAN_STAGE, SET_FAN_STATE, SET_FAN_MODE
    - Converts the raw sensor ID (e.g., "TSTAT_00") into a friendly name using sensor_map.
    """
    if not message.startswith("TSTAT_"):
        return
    try:
        parts = message.split()
        if len(parts) < 2:
            return
        zone_code = parts[0]
        # Convert raw sensor ID using sensor_map; fallback to zone_code if not found.
        zone = sensor_map.get(zone_code, zone_code)
        if "=" not in parts[1]:
            return
        param, value_str = parts[1].split("=")
        value = int(value_str)
    except Exception as e:
        print(f"Error parsing message '{message}': {e}")
        return

    ignore_params = {"SET_TEMP_TOLERANCE", "SET_HUMIDITY_SP", "SET_FAN_STAGE", "SET_FAN_STATE", "SET_FAN_MODE"}
    if param in ignore_params:
        return

    current_time = datetime.now().astimezone(ZoneInfo(TZ)).strftime('%Y-%m-%d %H:%M:%S')
    with db_lock:
        try:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO thermostat_logs (timestamp, zone, parameter, value, raw_message)
                    VALUES (?, ?, ?, ?, ?)
                """, (current_time, zone, param, value, message))
                conn.commit()
        except sqlite3.Error as e:
            print(f"Database error: {e}")

# =============================================================================
# TELNET LISTENER FUNCTION
# =============================================================================
async def listen_telnet():
    """
    Continuously connect to the thermostat via telnet,
    read messages, and process them using parse_message().
    Implements a retry/backoff strategy.
    """
    retry_delay = 1
    while True:
        try:
            print(f"Connecting to telnet {TELNET_IP}:{TELNET_PORT}")
            reader, writer = await telnetlib3.open_connection(TELNET_IP, TELNET_PORT, encoding="utf-8")
            retry_delay = 1  # Reset delay on success.
            while True:
                message = await reader.readline()
                if not message:
                    break
                message = message.strip()
                if message:
                    print(f"Received: {message}")
                    parse_message(message)
        except Exception as e:
            print(f"Telnet connection error: {e}. Retrying in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

# =============================================================================
# AGGREGATION FUNCTIONS FOR TEMPERATURE DATA
# =============================================================================
def get_usage_data(period):
    """
    Aggregate temperature data for the specified period.
    For "hourly", group logs by minute (using substr(timestamp, 1, 16))
    and compute the average SET_TEMP (dividing by 10 for °F) for each minute.
    Represents a rolling window of the past 1 hour.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    if period == "hourly":
        cutoff = (now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, substr(timestamp, 1, 16) as ts, AVG(value) as avg_temp
                    FROM thermostat_logs
                    WHERE timestamp >= ? AND parameter = 'SET_TEMP'
                    GROUP BY zone, ts
                    ORDER BY ts
                """, (cutoff,))
                for row in cursor.fetchall():
                    zone, ts, avg_temp = row
                    time_label = ts[11:16]
                    data.append({
                        "zone": zone,
                        "avg_temp": (avg_temp / 10.0) if avg_temp is not None else None,
                        "timestamp": time_label
                    })
    return data

def get_daily_usage_data():
    """
    Aggregate temperature data by hour for the past 24 hours.
    This represents a rolling window (e.g., if it's 6 pm, covers 6 pm yesterday to 6 pm today).
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(24):
        start = now - timedelta(hours=i+1)
        end = now - timedelta(hours=i)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_TEMP' THEN value END) AS avg_temp
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_temp = row
                    data.append({
                        "zone": zone,
                        "avg_temp": (avg_temp / 10.0) if avg_temp is not None else None,
                        "timestamp": end.strftime('%H:%M')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

def get_weekly_usage_data():
    """
    Aggregate temperature data by day for the past 7 days.
    For each day, calculate the average temperature.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(7):
        start = (now - timedelta(days=i+1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now - timedelta(days=i)).replace(hour=23, minute=59, second=59, microsecond=0)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_TEMP' THEN value END) AS avg_temp
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_temp = row
                    data.append({
                        "zone": zone,
                        "avg_temp": (avg_temp / 10.0) if avg_temp is not None else None,
                        "timestamp": (now - timedelta(days=i)).strftime('%Y-%m-%d')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

def get_monthly_usage_data():
    """
    Aggregate temperature data by day for the past 30 days.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(30):
        start = (now - timedelta(days=i+1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now - timedelta(days=i)).replace(hour=23, minute=59, second=59, microsecond=0)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_TEMP' THEN value END) AS avg_temp
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_temp = row
                    data.append({
                        "zone": zone,
                        "avg_temp": (avg_temp / 10.0) if avg_temp is not None else None,
                        "timestamp": (now - timedelta(days=i)).strftime('%Y-%m-%d')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

# =============================================================================
# AGGREGATION FUNCTIONS FOR HVAC STATE DATA
# =============================================================================
def get_hourly_hvac_data():
    """
    For each minute in the past hour, compute the average HVAC state.
    If the average is >= 0.5, return 1 (Running); otherwise, 0 (On Standby).
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(60):
        start = now - timedelta(minutes=i+1)
        end = now - timedelta(minutes=i)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_HVAC_STATE' THEN value END) AS avg_state
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_state = row
                    state = 1 if avg_state is not None and avg_state >= 0.5 else 0
                    data.append({
                        "zone": zone,
                        "state": state,
                        "timestamp": end.strftime('%H:%M')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

def get_daily_hvac_data():
    """
    Aggregate HVAC state data by hour for the past 24 hours.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(24):
        start = now - timedelta(hours=i+1)
        end = now - timedelta(hours=i)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_HVAC_STATE' THEN value END) AS avg_state
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_state = row
                    state = 1 if avg_state is not None and avg_state >= 0.5 else 0
                    data.append({
                        "zone": zone,
                        "state": state,
                        "timestamp": end.strftime('%H:%M')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

def get_weekly_hvac_data():
    """
    Aggregate HVAC state data by day for the past 7 days.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(7):
        start = (now - timedelta(days=i+1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now - timedelta(days=i)).replace(hour=23, minute=59, second=59, microsecond=0)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_HVAC_STATE' THEN value END) AS avg_state
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_state = row
                    state = 1 if avg_state is not None and avg_state >= 0.5 else 0
                    data.append({
                        "zone": zone,
                        "state": state,
                        "timestamp": (now - timedelta(days=i)).strftime('%Y-%m-%d')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

def get_monthly_hvac_data():
    """
    Aggregate HVAC state data by day for the past 30 days.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    data = []
    for i in range(30):
        start = (now - timedelta(days=i+1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now - timedelta(days=i)).replace(hour=23, minute=59, second=59, microsecond=0)
        with db_lock:
            with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT zone, AVG(CASE WHEN parameter = 'SET_HVAC_STATE' THEN value END) AS avg_state
                    FROM thermostat_logs
                    WHERE timestamp BETWEEN ? AND ?
                    GROUP BY zone
                """, (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')))
                for row in cursor.fetchall():
                    zone, avg_state = row
                    state = 1 if avg_state is not None and avg_state >= 0.5 else 0
                    data.append({
                        "zone": zone,
                        "state": state,
                        "timestamp": (now - timedelta(days=i)).strftime('%Y-%m-%d')
                    })
    return sorted(data, key=lambda x: x["timestamp"])

# =============================================================================
# (OPTIONAL) HVAC DUTY CYCLE AGGREGATION FUNCTIONS
# =============================================================================
def get_overall_hvac_duty_data(period):
    """
    Calculate overall HVAC duty cycle percentages over a given period.
    Periods: "hourly", "daily", "weekly", "monthly".
    Returns an object with keys: off, fan, cool, heat.
    Duty cycle percentages are calculated as:
         (count for state / total records) * 100.
    """
    now = datetime.now().astimezone(ZoneInfo(TZ))
    if period == "hourly":
        start = now - timedelta(hours=1)
    elif period == "daily":
        start = now - timedelta(hours=24)
    elif period == "weekly":
        start = now - timedelta(days=7)
    elif period == "monthly":
        start = now - timedelta(days=30)
    else:
        return {"off": 0.0, "fan": 0.0, "cool": 0.0, "heat": 0.0}
    
    with db_lock:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value FROM thermostat_logs 
                WHERE parameter = 'SET_HVAC_STATE'
                AND timestamp BETWEEN ? AND ?
            """, (start.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')))
            rows = cursor.fetchall()
    values = [row[0] for row in rows if row[0] in (0, 1, 2, 3)]
    total = len(values)
    if total == 0:
        return {"off": 0.0, "fan": 0.0, "cool": 0.0, "heat": 0.0}
    pct_off = round((values.count(0) / total) * 100, 1)
    pct_fan = round((values.count(1) / total) * 100, 1)
    pct_cool = round((values.count(2) / total) * 100, 1)
    pct_heat = round((values.count(3) / total) * 100, 1)
    return {"off": pct_off, "fan": pct_fan, "cool": pct_cool, "heat": pct_heat}

# =============================================================================
# API ENDPOINTS
# =============================================================================
@app.route("/current_status", methods=["GET"])
def current_status_endpoint():
    """
    Retrieve the latest status for each sensor zone.
    - Temperature is converted from tenths (SET_TEMP) to °F.
    - HVAC state is interpreted as: 0: Off, 1: Fan Only, 2: Cooling, 3: Running.
    The sensor friendly names are applied from sensor_map.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT zone, parameter, value, timestamp FROM thermostat_logs ORDER BY timestamp DESC")
        rows = cursor.fetchall()
    status = {}
    for zone, param, value, ts in rows:
        if zone not in status:
            status[zone] = {"zone": zone, "temperature": None, "hvac": None, "timestamp": ts}
        if param == "SET_TEMP" and status[zone]["temperature"] is None:
            status[zone]["temperature"] = value / 10.0
        if param == "SET_HVAC_STATE" and status[zone]["hvac"] is None:
            if value == 0:
                status[zone]["hvac"] = "Off"
            elif value == 1:
                status[zone]["hvac"] = "Fan Only"
            elif value == 2:
                status[zone]["hvac"] = "Cooling"
            elif value == 3:
                status[zone]["hvac"] = "Running"
            else:
                status[zone]["hvac"] = str(value)
    return jsonify(list(status.values()))

@app.route("/logs/day", methods=["GET"])
def logs_last_day():
    since_time = (datetime.now().astimezone(ZoneInfo(TZ)) - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    with db_lock:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM thermostat_logs WHERE timestamp >= ? ORDER BY timestamp DESC", (since_time,))
            logs = cursor.fetchall()
    return jsonify(logs)

# Temperature usage API endpoints.
@app.route("/api/hourly_usage")
def api_hourly_usage():
    data = get_usage_data("hourly")
    return jsonify(data)

@app.route("/api/daily_usage")
def api_daily_usage():
    data = get_daily_usage_data()
    return jsonify(data)

@app.route("/api/weekly_usage")
def api_weekly_usage():
    data = get_weekly_usage_data()
    return jsonify(data)

@app.route("/api/monthly_usage")
def api_monthly_usage():
    data = get_monthly_usage_data()
    return jsonify(data)

# HVAC state API endpoints.
@app.route("/api/hourly_hvac")
def api_hourly_hvac():
    data = get_hourly_hvac_data()
    return jsonify(data)

@app.route("/api/daily_hvac")
def api_daily_hvac():
    data = get_daily_hvac_data()
    return jsonify(data)

@app.route("/api/weekly_hvac")
def api_weekly_hvac():
    data = get_weekly_hvac_data()
    return jsonify(data)

@app.route("/api/monthly_hvac")
def api_monthly_hvac():
    data = get_monthly_hvac_data()
    return jsonify(data)

# HVAC duty cycle endpoints (overall percentages).
@app.route("/api/hourly_hvac_duty")
def api_hourly_hvac_duty():
    data = get_overall_hvac_duty_data("hourly")
    return jsonify(data)

@app.route("/api/daily_hvac_duty")
def api_daily_hvac_duty():
    data = get_overall_hvac_duty_data("daily")
    return jsonify(data)

@app.route("/api/weekly_hvac_duty")
def api_weekly_hvac_duty():
    data = get_overall_hvac_duty_data("weekly")
    return jsonify(data)

@app.route("/api/monthly_hvac_duty")
def api_monthly_hvac_duty():
    data = get_overall_hvac_duty_data("monthly")
    return jsonify(data)

# =============================================================================
# DASHBOARD ROUTES
# =============================================================================
@app.route("/")
def index():
    return render_template("hvac_dashboard.html")

@app.route("/dashboard")
def dashboard():
    return render_template("hvac_dashboard.html")

# =============================================================================
# MAIN: START THREADS AND RUN THE FLASK SERVER
# =============================================================================
if __name__ == "__main__":
    setup_database()  # Initialize the main database and table if not exists.
    
    # Start the telnet listener in a daemon thread.
    telnet_thread = threading.Thread(target=lambda: asyncio.run(listen_telnet()), daemon=True)
    telnet_thread.start()
    
    # Start the purge thread to clean out records older than 90 days.
    purge_thread = threading.Thread(target=purge_thread_function, daemon=True)
    purge_thread.start()
    
    # Run the Flask web server.
    app.run(host="0.0.0.0", port=5105, debug=True)
