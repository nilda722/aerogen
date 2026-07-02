import json
import os
import platform
import shutil
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn

    HARDWARE_IMPORTS_OK = True
    HARDWARE_IMPORT_ERROR = ""
except Exception as exc:
    board = busio = ADS = AnalogIn = None
    HARDWARE_IMPORTS_OK = False
    HARDWARE_IMPORT_ERROR = str(exc)


SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_AVAILABLE = bool(SUPABASE_URL and SUPABASE_KEY)

try:
    if SUPABASE_AVAILABLE:
        from supabase import create_client
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    else:
        _sb = None
except Exception:
    _sb = None
    SUPABASE_AVAILABLE = False


_memory_log = []
_settings = {
    "sampling_interval": 2,
    "battery_capacity_ah": 100,
    "battery_full_v": 14.4,
    "battery_empty_v": 11.0,
    "logging_enabled": True,
    "acs_sensitivity_mv": 66.0,
    "acs_vref": 2.50,
    "turbine_voltage_ratio": 6.0,
    "solar_voltage_ratio": 6.0,
    "battery_voltage_ratio": 6.0,
}
SETTINGS_FILE = os.environ.get("AEROGEN_SETTINGS_FILE", os.path.join(os.getcwd(), "aerogen_settings.json"))

_last_probe = {"time": 0.0, "status": None}
_last_read = {"time": 0.0, "data": None}
_last_log_time = 0.0


def _env_mode():
    return os.environ.get("AEROGEN_MODE", "hardware").strip().lower()


def load_saved_settings():
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        for key, value in saved.items():
            if key in _settings:
                _settings[key] = value
    except Exception as exc:
        app.logger.warning("Could not load settings file: %s", exc)


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(_settings, fh, indent=2)


def active_mode():
    return "hardware"


def _sensor(name, kind, status, detail, required=True):
    return {"name": name, "kind": kind, "status": status, "detail": detail, "required": required}


def probe_status(force=False):
    now = time.time()
    if not force and _last_probe["status"] and now - _last_probe["time"] < 5:
        return _last_probe["status"]

    sensors = []
    adc_ok = False

    if not HARDWARE_IMPORTS_OK:
        sensors.append(_sensor("Raspberry Pi hardware libraries", "hardware_imports", "missing", HARDWARE_IMPORT_ERROR))
    else:
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c)
            ads.gain = 1
            AnalogIn(ads, 0).voltage
            adc_ok = True
            sensors.append(_sensor("ADS1115 ADC", "adc", "ok", "I2C ADC detected"))
        except Exception as exc:
            sensors.append(_sensor("ADS1115 ADC", "adc", "missing", str(exc)))

        adc_detail = "ADS1115 channel available" if adc_ok else "Waiting for ADS1115"
        state = "ok" if adc_ok else "missing"
        sensors.extend([
            _sensor("Solar ACS712 30A", "solar_current", state, "ADS1115 A0 - " + adc_detail),
            _sensor("Turbine ACS712 30A", "turbine_current", "missing", "ADS1115 A1 is empty during solar current testing", required=False),
            _sensor("Turbine voltage divider", "turbine_voltage", state, "ADS1115 A2 - " + adc_detail),
            _sensor("Battery voltage monitor", "battery_voltage", state, "ADS1115 A3 - " + adc_detail),
            _sensor("Solar voltage divider", "solar_voltage", "missing", "Needs a dedicated ADC channel or second ADS1115", required=False),
        ])

    status = {
        "app": "AeroGen",
        "mode": active_mode(),
        "requested_mode": _env_mode(),
        "hardware_imports_ok": HARDWARE_IMPORTS_OK,
        "sensors": sensors,
        "system": system_stats(),
        "supabase": supabase_status(),
    }
    _last_probe.update({"time": now, "status": status})
    return status


def read_sensors():
    try:
        return read_hardware()
    except Exception as exc:
        app.logger.error("Hardware read failed: %s", exc)
        return empty_reading("hardware_error")


def read_hardware():
    if not HARDWARE_IMPORTS_OK:
        return empty_reading("hardware_missing")

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c)
    ads.gain = 1

    ch0 = AnalogIn(ads, 0)
    ch2 = AnalogIn(ads, 2)
    ch3 = AnalogIn(ads, 3)

    sensitivity = float(_settings["acs_sensitivity_mv"]) / 1000.0
    vref = float(_settings["acs_vref"])

    solar_i = (ch0.voltage - vref) / sensitivity
    turbine_i = None
    turbine_v = ch2.voltage * float(_settings["turbine_voltage_ratio"])
    battery_v = ch3.voltage * float(_settings["battery_voltage_ratio"])
    solar_v = None

    return package_reading(turbine_v, turbine_i, solar_v, solar_i, battery_v, "hardware")


def clean_current(value):
    if value is None or abs(float(value)) < 0.05:
        return None
    return float(value)


def clean_voltage(value):
    if value is None or float(value) <= 0.05:
        return None
    return float(value)


def round_or_none(value, digits):
    return None if value is None else round(value, digits)


def calc_power(voltage, current):
    if voltage is None or current is None:
        return None
    return max(0.0, voltage * current)


def empty_reading(mode):
    return package_reading(None, None, None, None, None, mode)


def package_reading(turbine_v, turbine_i, solar_v, solar_i, battery_v, mode):
    turbine_v = clean_voltage(turbine_v)
    solar_v = clean_voltage(solar_v)
    battery_v = clean_voltage(battery_v)
    turbine_i = clean_current(turbine_i)
    solar_i = clean_current(solar_i)

    turbine_p = calc_power(turbine_v, turbine_i)
    solar_p = calc_power(solar_v, solar_i)
    valid_powers = [p for p in (turbine_p, solar_p) if p is not None]
    total_power = sum(valid_powers) if valid_powers else None

    full = float(_settings["battery_full_v"])
    empty = float(_settings["battery_empty_v"])
    battery_soc = None
    if battery_v is not None and full > empty:
        battery_soc = max(0.0, min(100.0, (battery_v - empty) / (full - empty) * 100))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "turbine_voltage": round_or_none(turbine_v, 3),
        "turbine_current": round_or_none(turbine_i, 3),
        "turbine_power": round_or_none(turbine_p, 2),
        "solar_voltage": round_or_none(solar_v, 3),
        "solar_current": round_or_none(solar_i, 3),
        "solar_power": round_or_none(solar_p, 2),
        "total_power": round_or_none(total_power, 2),
        "battery_voltage": round_or_none(battery_v, 3),
        "battery_soc": round_or_none(battery_soc, 1),
        "system": system_stats(),
    }


def system_stats():
    total, used, free = shutil.disk_usage(os.getcwd())
    memory = _memory_stats()
    load = None
    if hasattr(os, "getloadavg"):
        try:
            load = round(os.getloadavg()[0], 2)
        except OSError:
            load = None
    return {
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uptime_s": round(time.monotonic()),
        "disk_used_pct": round((used / total) * 100, 1) if total else None,
        "memory_used_pct": memory,
        "load_1m": load,
    }


def _memory_stats():
    if os.name == "nt":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(float(stat.dwMemoryLoad), 1)
        except Exception:
            return None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            values = {}
            for line in fh:
                key, raw = line.split(":", 1)
                values[key] = float(raw.strip().split()[0])
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if total and available:
            return round(((total - available) / total) * 100, 1)
    except Exception:
        return None
    return None


def supabase_status():
    ok = False
    error = None
    if _sb:
        try:
            _sb.table("aerogen_logs").select("id").limit(1).execute()
            ok = True
        except Exception as exc:
            error = str(exc)
    return {
        "enabled": SUPABASE_AVAILABLE,
        "ok": ok,
        "error": error if error else (None if ok or not SUPABASE_AVAILABLE else "unreachable"),
    }


def log_reading(data):
    if not _settings.get("logging_enabled"):
        return
    if _sb:
        try:
            payload = {k: v for k, v in data.items() if k != "system"}
            _sb.table("aerogen_logs").insert(payload).execute()
            return
        except Exception as exc:
            app.logger.warning("Supabase insert failed: %s", exc)
    _memory_log.append(data)
    if len(_memory_log) > 5000:
        _memory_log.pop(0)


def fetch_logs(page=1, per_page=50):
    if _sb:
        try:
            result = (
                _sb.table("aerogen_logs")
                .select("*", count="exact")
                .order("created_at", desc=True)
                .range((page - 1) * per_page, page * per_page - 1)
                .execute()
            )
            return {"data": result.data, "count": result.count}
        except Exception:
            pass
    data = list(reversed(_memory_log))
    start = (page - 1) * per_page
    return {"data": data[start : start + per_page], "count": len(data)}


def today_utc_bounds():
    local_now = datetime.now().astimezone()
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def avg_metric(rows, key):
    values = []
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return round(sum(values) / len(values), 2) if values else None


def summarize_rows(rows):
    return {
        "date": datetime.now().astimezone().date().isoformat(),
        "count": len(rows),
        "avg_turbine_power": avg_metric(rows, "turbine_power"),
        "avg_solar_power": avg_metric(rows, "solar_power"),
        "avg_battery_soc": avg_metric(rows, "battery_soc"),
    }


def fetch_daily_summary():
    start, end = today_utc_bounds()
    if _sb:
        try:
            result = (
                _sb.table("aerogen_logs")
                .select("timestamp,turbine_power,solar_power,battery_soc")
                .gte("timestamp", start.isoformat())
                .lt("timestamp", end.isoformat())
                .execute()
            )
            return summarize_rows(result.data or [])
        except Exception:
            pass

    today_rows = []
    for row in _memory_log:
        ts = parse_timestamp(row.get("timestamp"))
        if ts and start <= ts.astimezone(timezone.utc) < end:
            today_rows.append(row)
    return summarize_rows(today_rows)


def get_reading(force=False):
    global _last_log_time
    now = time.time()
    interval = int(_settings.get("sampling_interval", 2))
    if force or _last_read["data"] is None or (now - _last_read["time"]) >= interval:
        data = read_sensors()
        _last_read.update({"time": now, "data": data})
    if _last_read["data"] is not None and (now - _last_log_time) >= interval:
        log_reading(_last_read["data"])
        _last_log_time = now
    return _last_read["data"]


load_saved_settings()


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/logs")
def logs():
    return render_template("logs.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/api/reading")
def api_reading():
    return jsonify(get_reading(force=request.args.get("fresh", "0") == "1"))


@app.route("/api/logs")
def api_logs():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    payload = fetch_logs(page, per_page)
    payload["daily_summary"] = fetch_daily_summary()
    return jsonify(payload)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        payload = request.get_json(force=True)
        allowed = set(_settings)
        for key, value in payload.items():
            if key in allowed:
                _settings[key] = value
        save_settings()
        probe_status(force=True)
        return jsonify({"ok": True, "settings": _settings})
    return jsonify(_settings)


@app.route("/api/status")
@app.route("/health")
def health():
    return jsonify(probe_status(force=request.args.get("fresh", "0") == "1"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

