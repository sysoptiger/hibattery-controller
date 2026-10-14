import json
import time
import urllib.request
import urllib.error
import threading
import os
import signal

from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
from collections import deque

import paho.mqtt.client as mqtt


# ============================================================
# HIBATTERY CONTROLLER 3.9.7
#
# NEU 3.9.7 GEGENÜBER 3.9.6
#
# 1. Ausfall des aktuellen Einzelpreises (Energy-Charts
#    price_current) legt die Steuerung nicht mehr lahm.
#    Bisher: kein Einzelpreis -> GENERAL ("Kein aktueller
#    Preis"), auch wenn der Tagesplan vorhanden war.
#    Jetzt: Preis der laufenden Viertelstunde aus dem
#    Tagesplan. Gesteuert wird ohnehin über den Tagesplan.
#
# 2. Nach einem Fehler beim Einzelpreis neuer Versuch erst
#    nach 2 Minuten statt alle 10 Sekunden (schont die API,
#    vermeidet HTTP 429).
#
# AUS 3.9.6
#
# 1. SOC-Ziel erreicht während PV-Zeit (Rang 1-16):
#    Bisher HOLD -> PV-Überschuss ging größtenteils ins Netz.
#    Jetzt übernimmt der PV-Schutz wie ab Rang 17:
#       Überschuss   -> GENERAL (Akku nimmt PV über das Ziel auf)
#       Unterdeckung -> nach 60 s HOLD
#    Außerhalb der PV-Zeit unverändert HOLD.
#
# 2. Konfiguration in /app/config.json:
#    Broker, Geräte-ID, Standort, PV-Anlage und Schwellwerte.
#    Fehlt die Datei oder ein Wert, gilt der Standardwert
#    aus diesem Code. Ungültige Werte werden geloggt und
#    ignoriert.
#
# 3. MQTT mit Benutzer und Passwort:
#    "mqtt": {"username": "...", "password": "..."} in der
#    config.json. Leer = ohne Anmeldung (wie bisher).
#    Das Passwort wird nie ins Log geschrieben.
#
# AUS 3.9.5
#
# 1. Preisinfo per MQTT für die Shelly-Plugs (Waschmaschinen):
#    Topic "hibattery/price" (retained), alle 60 s und bei
#    jedem Viertelstunden-Wechsel:
#
#    {"ok": true, "rank": 5, "n": 96, "price_ct": 21.34,
#     "slot_start": <unix>, "valid_until": <unix>}
#
#    rank = 1 ist die günstigste Viertelstunde des Tages.
#    Ohne HEUTE-Plan: {"ok": false, ...}
#
#    Die Akku-Steuerung selbst ist unverändert.
#
# AUS 3.9.4
#
# 1. Zeitumstellung Sommer/Winter:
#    Ein Tag hat 92, 96 oder 100 Viertelstunden.
#    Die erwartete Anzahl wird pro Tag aus der Zeitzone
#    berechnet (z.B. 25.10.2026 = 100, 28.03.2027 = 92).
#    Bisher wurden diese Tage verworfen -> ganzer Tag GENERAL.
#
#    Zeitvergleiche laufen über Unix-Timestamps, damit die
#    doppelte Stunde 02:00-03:00 im Oktober eindeutig ist.
#
#    "Sehr teuer" = die teuersten 24 Viertelstunden des Tages.
#    An normalen Tagen unverändert Rang 73-96.
#
# 2. PV-Prognose:
#    Open-Meteo liefert Strahlung als Mittel der
#    VORANGEGANGENEN Stunde. Für die aktuelle Zeit wird jetzt
#    der Wert der laufenden Stunde verwendet (bisher eine
#    Stunde zu spät). Abruf mit timeformat=unixtime.
#
# 3. SIGTERM (docker stop / Container-Neustart):
#    Controller beendet sauber und sendet EMS -> general.
#    Wirkt nur, wenn im Container-Command
#    "exec python /app/controller.py" steht.
#
# Steuerlogik ansonsten unverändert gegenüber 3.9.3.
#
# AUS 3.9.3
#
# 0. Rang 1-8 und 9-16 laden bis zum SOC-Ziel auch während PV-Zeit.
#    Rang 17-24 bevorzugt während PV-Zeit weiterhin PV.
#
# AUS 3.9.2
#
# 1. PV-Schutz wird außerhalb der PV-Zeit sicher zurückgesetzt
#
# 2. Höhere wetterabhängige SOC-Ziele:
#       gute PV     -> 75 %
#       mittlere PV -> 85 %
#       schlechte PV-> 95 %
#
# 3. Rang 25-72: Vorausschau bis 24 Stunden.
#       genug Reserve bis zum nächsten >=5 ct günstigeren
#       Ladefenster -> GENERAL, sonst HOLD
#
# 4. Rang 73-96: GENERAL / Entladen
#
# 5. PV-Regelung aus 3.9.1 bleibt erhalten.
# ============================================================


VERSION = "3.9.7"

BERLIN = ZoneInfo("Europe/Berlin")

# IP-Adresse des MQTT-Brokers (Mosquitto)
BROKER = "192.168.1.10"
PORT = 1883

# NEU 3.9.6: leer = ohne Anmeldung
MQTT_USERNAME = ""
MQTT_PASSWORD = ""

# Geräte-ID des Akkus, wie sie im MQTT-Topic erscheint:
# homeassistant/sensor/<MASTER>/quick/state
MASTER = "MSA-000000000000"

AUTO_CONTROL = True


# ============================================================
# SPEICHER
# ============================================================

BATTERY_CAPACITY_KWH = 10.0

SOC_MIN = 11.0

SOC_TARGET_FALLBACK = 85.0


# ============================================================
# HAUS / PLANUNG
# ============================================================

TYPICAL_HOUSE_LOAD_W = 350

# 15 % Aufschlag für:
# - Lastschwankungen
# - Verluste
# - Prognoseunsicherheit

HOUSE_RESERVE_FACTOR = 1.15


# ============================================================
# VORAUSSCHAUENDE PREISPLANUNG
# ============================================================

LOOKAHEAD_HOURS = 24

# Ein späterer Preis muss mindestens so viel günstiger sein,
# damit Entladen + späteres Nachladen wirtschaftlich
# interessant ist.

MIN_PRICE_ADVANTAGE_CT = 5.0


# ============================================================
# PV-ANLAGE
# ============================================================

PV_POWER_KWP = 0.8

# Standort der PV-Anlage (für die Wetterprognose)
PV_LATITUDE = 51.0
PV_LONGITUDE = 10.0

# Open-Meteo:
# 0 = Süd
# -90 = Ost
# +90 = West

PV_AZIMUTH = 0

# Neigung gegenüber der Horizontalen
# 0 = waagerecht, 90 = senkrecht

PV_TILT = 30

PV_SYSTEM_FACTOR = 0.85


# ============================================================
# PV-PROGNOSE -> SOC-ZIEL
# ============================================================

PV_GOOD_KWH = 2.5
PV_MEDIUM_KWH = 1.2

SOC_TARGET_PV_GOOD = 75.0
SOC_TARGET_PV_MEDIUM = 85.0
SOC_TARGET_PV_BAD = 95.0


# ============================================================
# NETZLADELEISTUNG
# ============================================================

MAX_CHARGE_POWER_W = 1500

CHARGE_POWER_HIGH = 1500
CHARGE_POWER_MEDIUM = 1000
CHARGE_POWER_LOW = 600


# ============================================================
# PREISLOGIK
# ============================================================

CHEAP_HIGH_COUNT = 8
CHEAP_MEDIUM_COUNT = 16
CHEAP_MAX_COUNT = 24

# NEU 3.9.4:
# Anzahl der teuersten Viertelstunden pro Tag.
# Bei 96 Viertelstunden = Rang 73-96 (wie bisher).

EXPENSIVE_COUNT = 24

# Länge einer Viertelstunde in Sekunden

SLOT_SECONDS = 900


# ============================================================
# PV-REGELUNG
# ============================================================

PV_RADIATION_ACTIVE_WM2 = 40

PV_POWER_MARGIN_W = 30

PV_DEFICIT_DELAY_SECONDS = 60
PV_SURPLUS_DELAY_SECONDS = 30


# ============================================================
# INTERVALLE
# ============================================================

COMMAND_INTERVAL_SECONDS = 20
HOLD_INTERVAL_SECONDS = 10

DATA_TIMEOUT_SECONDS = 10

CURRENT_PRICE_UPDATE_SECONDS = 900

# NEU 3.9.7: Wartezeit nach Fehler beim Einzelpreis
CURRENT_PRICE_RETRY_SECONDS = 120
PRICE_THREAD_SLEEP_SECONDS = 10

PV_FORECAST_UPDATE_SECONDS = 1800


# ============================================================
# DATEIEN
# ============================================================

LOG_FILE = "/app/hibattery.log"
PRICE_FILE = "/app/epex_prices.json"

# NEU 3.9.6
CONFIG_FILE = "/app/config.json"


# ============================================================
# NEU 3.9.6: KONFIGURATION AUS config.json
#
# Abschnitt / Schlüssel in der Datei -> Variable im Code.
# Fehlt ein Wert, bleibt der Standardwert oben stehen.
# ============================================================

CONFIG_MAP = {
    "mqtt": {
        "broker":                ("BROKER", str),
        "port":                  ("PORT", int),
        "username":              ("MQTT_USERNAME", str),
        "password":              ("MQTT_PASSWORD", str),
    },
    "geraet": {
        "master":                ("MASTER", str),
        "auto_control":          ("AUTO_CONTROL", bool),
    },
    "speicher": {
        "kapazitaet_kwh":        ("BATTERY_CAPACITY_KWH", float),
        "soc_min":               ("SOC_MIN", float),
        "soc_ziel_fallback":     ("SOC_TARGET_FALLBACK", float),
    },
    "haus": {
        "typische_last_w":       ("TYPICAL_HOUSE_LOAD_W", float),
        "reserve_faktor":        ("HOUSE_RESERVE_FACTOR", float),
    },
    "vorausschau": {
        "stunden":               ("LOOKAHEAD_HOURS", int),
        "min_preisvorteil_ct":   ("MIN_PRICE_ADVANTAGE_CT", float),
    },
    "pv_anlage": {
        "kwp":                   ("PV_POWER_KWP", float),
        "breitengrad":           ("PV_LATITUDE", float),
        "laengengrad":           ("PV_LONGITUDE", float),
        "azimut":                ("PV_AZIMUTH", float),
        "neigung":               ("PV_TILT", float),
        "systemfaktor":          ("PV_SYSTEM_FACTOR", float),
    },
    "pv_ladeziel": {
        "gut_ab_kwh":            ("PV_GOOD_KWH", float),
        "mittel_ab_kwh":         ("PV_MEDIUM_KWH", float),
        "ziel_gut":              ("SOC_TARGET_PV_GOOD", float),
        "ziel_mittel":           ("SOC_TARGET_PV_MEDIUM", float),
        "ziel_schlecht":         ("SOC_TARGET_PV_BAD", float),
    },
    "ladeleistung": {
        "hoch_w":                ("CHARGE_POWER_HIGH", int),
        "mittel_w":              ("CHARGE_POWER_MEDIUM", int),
        "niedrig_w":             ("CHARGE_POWER_LOW", int),
    },
    "preislogik": {
        "guenstig_hoch_anzahl":  ("CHEAP_HIGH_COUNT", int),
        "guenstig_mittel_anzahl": ("CHEAP_MEDIUM_COUNT", int),
        "guenstig_max_anzahl":   ("CHEAP_MAX_COUNT", int),
        "teuer_anzahl":          ("EXPENSIVE_COUNT", int),
    },
    "pv_regelung": {
        "strahlung_aktiv_wm2":   ("PV_RADIATION_ACTIVE_WM2", float),
        "leistungsmarge_w":      ("PV_POWER_MARGIN_W", float),
        "unterdeckung_s":        ("PV_DEFICIT_DELAY_SECONDS", int),
        "ueberschuss_s":         ("PV_SURPLUS_DELAY_SECONDS", int),
    },
}

# Meldungen beim Laden. log() gibt es hier noch nicht,
# sie werden beim Start ins Log geschrieben.
CONFIG_MESSAGES = []


def convert_config_value(value, wanted):

    # bool ist in Python auch ein int -> getrennt prüfen

    if wanted is bool:
        if isinstance(value, bool):
            return value
        raise ValueError("true oder false erwartet")

    if isinstance(value, bool):
        raise ValueError("Zahl oder Text erwartet")

    if wanted is str:
        if isinstance(value, str):
            return value
        raise ValueError("Text erwartet")

    if wanted is int:
        if isinstance(value, int):
            return value
        if (
            isinstance(value, float)
            and value.is_integer()
        ):
            return int(value)
        raise ValueError("ganze Zahl erwartet")

    if wanted is float:
        if isinstance(value, (int, float)):
            return float(value)
        raise ValueError("Zahl erwartet")

    raise ValueError("unbekannter Typ")


def load_config():

    if not os.path.exists(CONFIG_FILE):

        CONFIG_MESSAGES.append(
            f"Keine {CONFIG_FILE} | "
            "Standardwerte aus dem Code"
        )

        return

    try:

        with open(
            CONFIG_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            raw = json.load(f)

    except Exception as e:

        CONFIG_MESSAGES.append(
            f"FEHLER {CONFIG_FILE} nicht lesbar: {e} | "
            "Standardwerte aus dem Code"
        )

        return

    if not isinstance(raw, dict):

        CONFIG_MESSAGES.append(
            f"FEHLER {CONFIG_FILE}: kein JSON-Objekt | "
            "Standardwerte aus dem Code"
        )

        return

    applied = 0

    for section, values in raw.items():

        # Abschnitte mit "_" am Anfang sind Kommentare
        if section.startswith("_"):
            continue

        mapping = CONFIG_MAP.get(section)

        if mapping is None or not isinstance(values, dict):

            CONFIG_MESSAGES.append(
                f"Config: Abschnitt '{section}' "
                "unbekannt, ignoriert"
            )

            continue

        for key, value in values.items():

            if key.startswith("_"):
                continue

            target = mapping.get(key)

            if target is None:

                CONFIG_MESSAGES.append(
                    f"Config: '{section}.{key}' "
                    "unbekannt, ignoriert"
                )

                continue

            name, wanted = target

            try:

                globals()[name] = convert_config_value(
                    value,
                    wanted
                )

                applied += 1

            except ValueError as e:

                CONFIG_MESSAGES.append(
                    f"Config: '{section}.{key}' "
                    f"ungültig ({e}), "
                    "Standardwert bleibt"
                )

    CONFIG_MESSAGES.append(
        f"Config geladen: {CONFIG_FILE} | "
        f"{applied} Werte"
    )


def check_config():

    # Unsinnige Kombinationen auf Standard zurücksetzen,
    # damit die Steuerung nie mit kaputten Werten läuft.

    global CHEAP_HIGH_COUNT
    global CHEAP_MEDIUM_COUNT
    global CHEAP_MAX_COUNT
    global PV_MEDIUM_KWH
    global PV_GOOD_KWH

    if not (
        0 < CHEAP_HIGH_COUNT
        <= CHEAP_MEDIUM_COUNT
        <= CHEAP_MAX_COUNT
        < 96 - EXPENSIVE_COUNT
    ):

        CONFIG_MESSAGES.append(
            "Config: Preisrang-Anzahlen passen nicht "
            "zusammen | Standard 8/16/24 verwendet"
        )

        CHEAP_HIGH_COUNT = 8
        CHEAP_MEDIUM_COUNT = 16
        CHEAP_MAX_COUNT = 24

    if PV_MEDIUM_KWH > PV_GOOD_KWH:

        CONFIG_MESSAGES.append(
            "Config: pv_ladeziel mittel > gut | "
            "Standard 1.2/2.5 kWh verwendet"
        )

        PV_MEDIUM_KWH = 1.2
        PV_GOOD_KWH = 2.5


load_config()
check_config()


# ============================================================
# MQTT TOPICS
# ============================================================

TOPIC_STATE = (
    f"homeassistant/sensor/"
    f"{MASTER}/quick/state"
)

TOPIC_EMS_MODE = (
    f"homeassistant/select/"
    f"{MASTER}/ems_mode/command"
)

TOPIC_POWER = (
    f"homeassistant/number/"
    f"{MASTER}/power_ctrl/set"
)

# NEU 3.9.5: Preisinfo für Shelly-Plugs

TOPIC_PRICE_INFO = "hibattery/price"

PRICE_PUBLISH_SECONDS = 60


# ============================================================
# ENERGY CHARTS
# ============================================================

PRICE_CURRENT_URL = (
    "https://api.energy-charts.info/v2/"
    "price_current?bzn=DE-LU"
)

PRICE_DAY_URL = (
    "https://api.energy-charts.info/price"
)


# ============================================================
# OPEN-METEO
# ============================================================

PV_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={PV_LATITUDE}"
    f"&longitude={PV_LONGITUDE}"
    "&hourly=global_tilted_irradiance"
    f"&tilt={PV_TILT}"
    f"&azimuth={PV_AZIMUTH}"
    "&timezone=Europe%2FBerlin"
    "&timeformat=unixtime"
    "&forecast_days=3"
)


USER_AGENT = f"HiBattery-Control/{VERSION}"


# ============================================================
# LIVE STATUS
# ============================================================

current_soc = None
current_status = None

current_battery_power = None
current_house_load = None
current_grid_power = None
current_pv_power = None

last_state_received = 0


# ============================================================
# PREISE
# ============================================================

current_price_ct = None
last_current_price_update = 0

# NEU 3.9.7: "live" = Energy-Charts, "plan" = Tagesplan
current_price_source = None

price_days = {}

last_day_ahead_attempt = 0
last_today_attempt = 0


# ============================================================
# CONTROLLER STATUS
# ============================================================

last_command_time = 0
last_command_power = None

controller_has_control = False
last_control_mode = None


# ============================================================
# NEU 3.9.5: MQTT PREISINFO
# ============================================================

mqtt_client = None

last_price_publish = 0
last_published_slot = None


# ============================================================
# HAUSLAST HISTORIE
# ============================================================

LOAD_HISTORY = deque(maxlen=300)

price_lock = threading.Lock()


# ============================================================
# PV STATUS
# ============================================================

pv_forecast = {}

# NEU 3.9.4:
# Schlüssel = Unix-Zeit des STUNDENENDES,
# Wert = mittlere GTI der Stunde davor.

pv_hourly = {}

last_pv_forecast_update = 0

pv_deficit_since = None
pv_surplus_since = None

pv_protection_active = False


# ============================================================
# ZEIT
# ============================================================

def now_berlin():
    return datetime.now(BERLIN)


def today_date():
    return now_berlin().date()


def tomorrow_date():
    return today_date() + timedelta(days=1)


def today_string():
    return today_date().strftime("%Y-%m-%d")


def tomorrow_string():
    return tomorrow_date().strftime("%Y-%m-%d")


def day_string_to_date(day_string):

    return datetime.strptime(
        day_string,
        "%Y-%m-%d"
    ).date()


# ============================================================
# NEU 3.9.4: VIERTELSTUNDEN PRO TAG
#
# Normaler Tag:          96
# Ende Sommerzeit (Okt): 100
# Beginn Sommerzeit (Mär): 92
# ============================================================

def expected_slots(day_date):

    start = datetime.combine(
        day_date,
        dt_time(0, 0),
        tzinfo=BERLIN
    )

    end = datetime.combine(
        day_date + timedelta(days=1),
        dt_time(0, 0),
        tzinfo=BERLIN
    )

    seconds = (
        end.timestamp()
        - start.timestamp()
    )

    return int(
        round(seconds / SLOT_SECONDS)
    )


# ============================================================
# LOG
# ============================================================

def log(message=""):

    timestamp = now_berlin().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    line = f"{timestamp} | {message}"

    print(line, flush=True)

    try:

        with open(
            LOG_FILE,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(line + "\n")

    except Exception as e:

        print(
            f"Logdatei Fehler: {e}",
            flush=True
        )


# ============================================================
# PREISDATEI
# ============================================================

def save_price_file():

    try:

        serializable = {}

        for day, entries in price_days.items():

            serializable[day] = []

            for entry in entries:

                serializable[day].append(
                    {
                        "timestamp": entry["timestamp"],
                        "price": entry["price"]
                    }
                )

        temp_file = PRICE_FILE + ".tmp"

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                serializable,
                f,
                indent=2
            )

        os.replace(
            temp_file,
            PRICE_FILE
        )

    except Exception as e:

        log(
            f"Preisdatei speichern Fehler: {e}"
        )


def load_price_file():

    global price_days

    if not os.path.exists(PRICE_FILE):

        log(
            "Noch keine gespeicherte Preisdatei vorhanden."
        )

        return

    try:

        with open(
            PRICE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            raw = json.load(f)

        loaded = {}

        for day, entries in raw.items():

            try:

                expected = expected_slots(
                    day_string_to_date(day)
                )

            except ValueError:

                continue

            converted = []

            for entry in entries:

                unix_time = int(
                    entry["timestamp"]
                )

                converted.append(
                    {
                        "timestamp":
                            unix_time,

                        "datetime":
                            datetime.fromtimestamp(
                                unix_time,
                                tz=BERLIN
                            ),

                        "price":
                            float(entry["price"])
                    }
                )

            converted.sort(
                key=lambda x:
                    x["timestamp"]
            )

            if len(converted) == expected:

                loaded[day] = converted

        price_days = loaded

        log(
            f"Preisdatei geladen: "
            f"{', '.join(sorted(price_days.keys()))}"
        )

    except Exception as e:

        log(
            f"Preisdatei laden Fehler: {e}"
        )


def cleanup_price_days():

    global price_days

    keep = {
        today_string(),
        tomorrow_string()
    }

    old_keys = [
        day
        for day in price_days
        if day not in keep
    ]

    if not old_keys:
        return

    for day in old_keys:
        del price_days[day]

    save_price_file()


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def eur_mwh_to_ct_kwh(value):

    return float(value) / 10.0


def average_house_load():

    if not LOAD_HISTORY:
        return None

    return (
        sum(LOAD_HISTORY)
        / len(LOAD_HISTORY)
    )


def measurements_valid():

    if current_soc is None:
        return False

    if last_state_received == 0:
        return False

    if (
        time.time()
        - last_state_received
        > DATA_TIMEOUT_SECONDS
    ):
        return False

    if (
        current_soc < 0
        or current_soc > 100
    ):
        return False

    return True


def get_day_plan(day_string):

    entries = price_days.get(
        day_string
    )

    if entries is None:
        return None

    try:

        expected = expected_slots(
            day_string_to_date(day_string)
        )

    except ValueError:

        return None

    if len(entries) != expected:
        return None

    return entries


# ============================================================
# PV PROGNOSE
# ============================================================

def fetch_pv_forecast():

    global pv_forecast
    global pv_hourly
    global last_pv_forecast_update

    try:

        request = urllib.request.Request(
            PV_FORECAST_URL,
            headers={
                "User-Agent":
                    USER_AGENT
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            data = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        hourly = data.get(
            "hourly",
            {}
        )

        times = hourly.get(
            "time",
            []
        )

        radiation = hourly.get(
            "global_tilted_irradiance",
            []
        )

        if (
            not times
            or
            not radiation
            or
            len(times) != len(radiation)
        ):

            log(
                "PV-Prognose: unvollständige Daten"
            )

            return False

        daily_irradiation = {}
        hourly_result = {}

        for unix_time, watts_m2 in zip(
            times,
            radiation
        ):

            if watts_m2 is None:
                continue

            # NEU 3.9.4:
            # Open-Meteo-Wert gilt für die Stunde VOR
            # diesem Zeitstempel.

            hour_end = int(unix_time)

            watts_m2 = float(
                watts_m2
            )

            hourly_result[hour_end] = watts_m2

            # Tageszuordnung über die Mitte der Stunde

            day = datetime.fromtimestamp(
                hour_end - 1800,
                tz=BERLIN
            ).date().strftime(
                "%Y-%m-%d"
            )

            if day not in daily_irradiation:

                daily_irradiation[day] = 0.0

            daily_irradiation[day] += (
                watts_m2
            )

        result = {}

        for day, wh_m2 in (
            daily_irradiation.items()
        ):

            irradiation_kwh_m2 = (
                wh_m2 / 1000.0
            )

            estimated_kwh = (
                irradiation_kwh_m2
                * PV_POWER_KWP
                * PV_SYSTEM_FACTOR
            )

            result[day] = {
                "irradiation":
                    irradiation_kwh_m2,

                "pv_kwh":
                    estimated_kwh
            }

        pv_forecast = result
        pv_hourly = hourly_result

        last_pv_forecast_update = (
            time.time()
        )

        log(
            "PV-Prognose aktualisiert | "
            f"West {PV_AZIMUTH}° | "
            f"Neigung {PV_TILT}° | "
            f"{PV_POWER_KWP:.1f} kWp"
        )

        log_pv_forecast(
            today_string()
        )

        log_pv_forecast(
            tomorrow_string()
        )

        return True

    except urllib.error.HTTPError as e:

        log(
            f"PV-Prognose HTTP-Fehler: "
            f"{e.code}"
        )

    except Exception as e:

        log(
            f"PV-Prognose Fehler: {e}"
        )

    return False


# ============================================================
# AKTUELLE PV-PROGNOSE
#
# NEU 3.9.4:
# Laufende Stunde = Wert mit Zeitstempel am ENDE
# der laufenden Stunde.
# Beispiel 18:40 -> Wert "19:00" (Mittel 18:00-19:00)
# ============================================================

def current_forecast_radiation():

    now_ts = int(time.time())

    hour_end = (
        (now_ts // 3600) * 3600
        + 3600
    )

    return pv_hourly.get(
        hour_end,
        0.0
    )


def forecast_pv_active():

    return (
        current_forecast_radiation()
        >= PV_RADIATION_ACTIVE_WM2
    )


# ============================================================
# PV-ZIEL
# ============================================================

def pv_target_for_day(day_string):

    data = pv_forecast.get(
        day_string
    )

    if data is None:

        return (
            SOC_TARGET_FALLBACK,
            "KEINE PROGNOSE",
            None
        )

    pv_kwh = data["pv_kwh"]

    if pv_kwh >= PV_GOOD_KWH:

        return (
            SOC_TARGET_PV_GOOD,
            "GUT",
            pv_kwh
        )

    if pv_kwh >= PV_MEDIUM_KWH:

        return (
            SOC_TARGET_PV_MEDIUM,
            "MITTEL",
            pv_kwh
        )

    return (
        SOC_TARGET_PV_BAD,
        "SCHLECHT",
        pv_kwh
    )


def log_pv_forecast(day_string):

    target, pv_class, pv_kwh = (
        pv_target_for_day(
            day_string
        )
    )

    if pv_kwh is None:

        log(
            f"PV {day_string}: "
            f"keine Prognose | "
            f"Fallback-Ziel {target:.0f}%"
        )

        return

    irradiation = (
        pv_forecast[day_string]
        ["irradiation"]
    )

    log(
        f"PV {day_string}: "
        f"{pv_kwh:.2f} kWh erwartet | "
        f"GTI={irradiation:.2f} kWh/m² | "
        f"{pv_class} | "
        f"Ladeziel {target:.0f}%"
    )


def ensure_pv_forecast():

    if (
        pv_forecast
        and
        time.time()
        - last_pv_forecast_update
        < PV_FORECAST_UPDATE_SECONDS
    ):
        return

    fetch_pv_forecast()


# ============================================================
# PV LIVE BILANZ
# ============================================================

def pv_balance():

    if current_pv_power is None:
        return None

    if current_house_load is None:
        return None

    return (
        current_pv_power
        - current_house_load
    )


# ============================================================
# PV-SCHUTZ
# ============================================================

def update_pv_protection():

    global pv_deficit_since
    global pv_surplus_since
    global pv_protection_active

    # Keine PV-Zeit: Schutz MUSS False sein.

    if not forecast_pv_active():

        pv_deficit_since = None
        pv_surplus_since = None

        if pv_protection_active:

            log(
                "PV-SCHUTZ AUS | "
                "keine aktive PV-Zeit"
            )

        pv_protection_active = False

        return False


    balance = pv_balance()

    if balance is None:

        return pv_protection_active


    now = time.time()


    # ========================================================
    # PV UNTERDECKUNG
    # ========================================================

    if balance <= -PV_POWER_MARGIN_W:

        pv_surplus_since = None

        if pv_deficit_since is None:

            pv_deficit_since = now

        elapsed = (
            now - pv_deficit_since
        )

        if (
            elapsed
            >= PV_DEFICIT_DELAY_SECONDS
        ):

            if not pv_protection_active:

                log(
                    "PV-SCHUTZ AKTIV | "
                    f"PV={current_pv_power:.0f}W | "
                    f"Haus={current_house_load:.0f}W | "
                    f"Bilanz={balance:.0f}W"
                )

            pv_protection_active = True


    # ========================================================
    # PV ÜBERSCHUSS
    # ========================================================

    elif balance >= PV_POWER_MARGIN_W:

        pv_deficit_since = None

        if pv_surplus_since is None:

            pv_surplus_since = now

        elapsed = (
            now - pv_surplus_since
        )

        if (
            elapsed
            >= PV_SURPLUS_DELAY_SECONDS
        ):

            if pv_protection_active:

                log(
                    "PV-SCHUTZ AUS | "
                    f"PV={current_pv_power:.0f}W | "
                    f"Haus={current_house_load:.0f}W | "
                    f"Bilanz=+{balance:.0f}W"
                )

            pv_protection_active = False


    return pv_protection_active


# ============================================================
# EPEX TAG
# ============================================================

def fetch_day(day_date):

    day_string = day_date.strftime(
        "%Y-%m-%d"
    )

    expected = expected_slots(
        day_date
    )

    url = (
        f"{PRICE_DAY_URL}"
        f"?bzn=DE-LU"
        f"&start={day_string}"
        f"&end={day_string}"
    )

    log(
        f"EPEX Tagesabruf: {day_string}"
    )

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    USER_AGENT
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            data = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        unix_seconds = data.get(
            "unix_seconds",
            []
        )

        prices = data.get(
            "price",
            []
        )

        if len(unix_seconds) != len(prices):

            log(
                f"EPEX {day_string}: "
                f"{len(unix_seconds)} Zeitwerte / "
                f"{len(prices)} Preise"
            )

            return None

        entries = []

        for unix_time, price in zip(
            unix_seconds,
            prices
        ):

            if price is None:
                continue

            dt = datetime.fromtimestamp(
                int(unix_time),
                tz=BERLIN
            )

            if dt.date() != day_date:
                continue

            entries.append(
                {
                    "timestamp":
                        int(unix_time),

                    "datetime":
                        dt,

                    "price":
                        eur_mwh_to_ct_kwh(
                            price
                        )
                }
            )

        entries.sort(
            key=lambda x:
                x["timestamp"]
        )

        log(
            f"EPEX {day_string}: "
            f"{len(entries)}/{expected} Werte"
        )

        if len(entries) != expected:
            return None

        return entries

    except urllib.error.HTTPError as e:

        log(
            f"EPEX {day_string} "
            f"HTTP-Fehler: {e.code}"
        )

    except Exception as e:

        log(
            f"EPEX {day_string} Fehler: {e}"
        )

    return None


def store_day_plan(
    day_string,
    entries
):

    price_days[day_string] = entries

    save_price_file()

    log(
        f"Tagesplan gespeichert: "
        f"{day_string} "
        f"({len(entries)}/{len(entries)})"
    )


# ============================================================
# AKTUELLER PREIS
# ============================================================

def plan_price_now():

    # NEU 3.9.7: Preis der laufenden Viertelstunde
    # aus dem Tagesplan (oder None).

    rank_info = current_price_rank()

    if rank_info is None:
        return None

    return rank_info[1]


def get_current_price(force=False):

    global current_price_ct
    global last_current_price_update
    global current_price_source

    now = time.time()

    if (
        not force
        and
        now - last_current_price_update
        < CURRENT_PRICE_UPDATE_SECONDS
    ):

        return current_price_ct

    try:

        request = urllib.request.Request(
            PRICE_CURRENT_URL,
            headers={
                "User-Agent":
                    USER_AGENT
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=10
        ) as response:

            data = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )

        value = (
            data["data"][0]
            ["values"]
            ["day_ahead_price"]
        )

        current_price_ct = (
            eur_mwh_to_ct_kwh(
                value
            )
        )

        last_current_price_update = now

        if current_price_source != "live":
            log(
                "EPEX aktuell: wieder von Energy-Charts"
            )

        current_price_source = "live"

        log(
            f"EPEX aktuell: "
            f"{current_price_ct:.2f} ct/kWh"
        )

    except Exception as e:

        # NEU 3.9.7: nächster Versuch erst nach
        # CURRENT_PRICE_RETRY_SECONDS

        last_current_price_update = (
            now
            - CURRENT_PRICE_UPDATE_SECONDS
            + CURRENT_PRICE_RETRY_SECONDS
        )

        fallback = plan_price_now()

        if fallback is not None:

            current_price_ct = fallback

            if current_price_source != "plan":
                log(
                    "EPEX aktuell: Ersatz aus Tagesplan"
                )

            current_price_source = "plan"

        log(
            f"EPEX aktuell Fehler: {e} | "
            f"neuer Versuch in "
            f"{CURRENT_PRICE_RETRY_SECONDS}s"
            + (
                f" | Tagesplan {fallback:.2f} ct/kWh"
                if fallback is not None
                else ""
            )
        )

    return current_price_ct


# ============================================================
# PREISRANG
#
# NEU 3.9.4:
# Suche der aktuellen Viertelstunde über Unix-Zeit.
# Rückgabe zusätzlich: Anzahl Viertelstunden des Tages.
# ============================================================

def current_price_rank():

    entries = get_day_plan(
        today_string()
    )

    if entries is None:
        return None

    now_ts = time.time()

    current_entry = None

    for entry in entries:

        start = entry["timestamp"]

        if (
            start
            <= now_ts
            < start + SLOT_SECONDS
        ):

            current_entry = entry
            break

    if current_entry is None:
        return None

    sorted_entries = sorted(
        entries,
        key=lambda x:
            x["price"]
    )

    for rank, entry in enumerate(
        sorted_entries
    ):

        if (
            entry["timestamp"]
            == current_entry["timestamp"]
        ):

            return (
                rank,
                current_entry["price"],
                current_entry["datetime"],
                len(entries)
            )

    return None


# ============================================================
# 24-STUNDEN PREISLISTE
# ============================================================

def future_price_entries():

    now_ts = time.time()

    end_ts = (
        now_ts
        + LOOKAHEAD_HOURS * 3600
    )

    result = []

    for day in (
        today_string(),
        tomorrow_string()
    ):

        entries = get_day_plan(day)

        if entries is None:
            continue

        for entry in entries:

            ts = entry["timestamp"]

            if (
                ts > now_ts
                and
                ts <= end_ts
            ):

                result.append(entry)

    result.sort(
        key=lambda x:
            x["timestamp"]
    )

    return result


# ============================================================
# NÄCHSTE ATTRAKTIVE LADEGELEGENHEIT
#
# Wir suchen einen zukünftigen Preis,
# der mindestens 5 ct/kWh günstiger als JETZT ist.
#
# Zusätzlich muss diese Viertelstunde zu den
# 24 günstigsten des jeweiligen Tages gehören.
# ============================================================

def find_next_attractive_charge(
    current_price
):

    future = future_price_entries()

    cheap_cache = {}

    for entry in future:

        day_string = (
            entry["datetime"]
            .date()
            .strftime("%Y-%m-%d")
        )

        if day_string not in cheap_cache:

            day_entries = get_day_plan(
                day_string
            )

            if day_entries is None:

                cheap_cache[day_string] = set()

            else:

                sorted_day = sorted(
                    day_entries,
                    key=lambda x:
                        x["price"]
                )

                cheap_cache[day_string] = {
                    x["timestamp"]
                    for x in sorted_day[
                        :CHEAP_MAX_COUNT
                    ]
                }

        if (
            entry["timestamp"]
            not in cheap_cache[day_string]
        ):
            continue

        advantage = (
            current_price
            - entry["price"]
        )

        if (
            advantage
            >= MIN_PRICE_ADVANTAGE_CT
        ):

            return entry

    return None


# ============================================================
# BENÖTIGTE SOC-RESERVE
#
# Beispiel:
#
# 6 Stunden
# 350 W Hauslast
# 15 % Aufschlag
#
# 0,35 * 6 * 1,15 = 2,415 kWh
#
# Bei 10 kWh Akku = 24,15 %
#
# + SOC_MIN 11 %
#
# -> Reserve ca. 35 %
#
# NEU 3.9.4: Stunden über Unix-Zeit (korrekt bei
# Zeitumstellung).
# ============================================================

def calculate_required_soc(
    target_time
):

    hours = (
        target_time.timestamp()
        - time.time()
    ) / 3600.0

    if hours < 0:
        hours = 0


    required_kwh = (
        (TYPICAL_HOUSE_LOAD_W / 1000.0)
        * hours
        * HOUSE_RESERVE_FACTOR
    )


    required_soc_energy = (
        required_kwh
        / BATTERY_CAPACITY_KWH
        * 100.0
    )


    required_soc = (
        SOC_MIN
        + required_soc_energy
    )


    required_soc = min(
        100.0,
        required_soc
    )


    return (
        required_soc,
        required_kwh,
        hours
    )


# ============================================================
# VORAUSSCHAUENDE ENTLADEENTSCHEIDUNG
# ============================================================

def predictive_discharge_decision(
    current_price,
    rank,
    n_slots
):

    next_charge = (
        find_next_attractive_charge(
            current_price
        )
    )


    # Kein deutlich günstigeres Ladefenster gefunden:
    # Akku lieber reservieren.

    if next_charge is None:

        return (
            False,
            (
                f"Preisrang {rank + 1}/{n_slots} | "
                f"{current_price:.2f} ct/kWh | "
                "kein >=5ct günstigeres Ladefenster "
                "in 24h | Akku reservieren"
            )
        )


    required_soc, required_kwh, hours = (
        calculate_required_soc(
            next_charge["datetime"]
        )
    )


    # SOC liegt über Reserve:
    # Akku darf verwendet werden.

    if current_soc > required_soc:

        return (
            True,
            (
                f"Vorausschau | "
                f"SOC={current_soc:.1f}% > "
                f"Reserve={required_soc:.1f}% | "
                f"nächstes günstiges Laden "
                f"{next_charge['datetime'].strftime('%d.%m %H:%M')} "
                f"mit {next_charge['price']:.2f} ct | "
                f"in {hours:.1f}h | "
                f"Bedarf={required_kwh:.2f}kWh"
            )
        )


    # Reserve erreicht:
    # HOLD.

    return (
        False,
        (
            f"Vorausschau | "
            f"SOC={current_soc:.1f}% <= "
            f"Reserve={required_soc:.1f}% | "
            f"bis {next_charge['datetime'].strftime('%d.%m %H:%M')} | "
            f"Bedarf={required_kwh:.2f}kWh | "
            "Akku reservieren"
        )
    )


# ============================================================
# LADEZIEL
# ============================================================

def current_soc_target():

    target, pv_class, pv_kwh = (
        pv_target_for_day(
            today_string()
        )
    )

    return target


# ============================================================
# STEUERENTSCHEIDUNG
# ============================================================

def desired_control():

    # PV-Schutz aktualisieren. Außerhalb der PV-Zeit wird er
    # dabei zuverlässig zurückgesetzt.
    update_pv_protection()

    rank_info = current_price_rank()

    if rank_info is None:
        return (
            "GENERAL",
            None,
            "Aktuelle Viertelstunde nicht gefunden"
        )

    rank, price, slot, n_slots = rank_info
    target_soc = current_soc_target()

    # NEU 3.9.4: teuerste 24 Viertelstunden des Tages
    expensive_start = n_slots - EXPENSIVE_COUNT

    # ========================================================
    # 1. SOC MINIMUM
    # ========================================================
    if current_soc <= SOC_MIN:

        # Bei günstigen Preisen wird auch während PV-Zeit
        # nachgeladen. Sicherheit vor PV-Optimierung.
        if rank < CHEAP_HIGH_COUNT:
            return (
                "CHARGE",
                -CHARGE_POWER_HIGH,
                (
                    f"SOC-Minimum | "
                    f"Preisrang {rank + 1}/{n_slots} | "
                    f"Ziel {target_soc:.0f}%"
                )
            )

        if rank < CHEAP_MEDIUM_COUNT:
            return (
                "CHARGE",
                -CHARGE_POWER_MEDIUM,
                (
                    f"SOC-Minimum | "
                    f"Preisrang {rank + 1}/{n_slots} | "
                    f"Ziel {target_soc:.0f}%"
                )
            )

        if rank < CHEAP_MAX_COUNT:
            return (
                "CHARGE",
                -CHARGE_POWER_LOW,
                (
                    f"SOC-Minimum | "
                    f"Preisrang {rank + 1}/{n_slots} | "
                    f"Ziel {target_soc:.0f}%"
                )
            )

        return (
            "HOLD",
            0,
            (
                f"SOC-Minimum {SOC_MIN:.0f}% | "
                "Entladung gesperrt"
            )
        )

    # ========================================================
    # 2. SEHR TEUER - teuerste 24 (normal Rang 73-96)
    # ========================================================
    if rank >= expensive_start:
        return (
            "GENERAL",
            None,
            (
                f"Preisrang {rank + 1}/{n_slots} | "
                f"{price:.2f} ct/kWh | "
                "sehr teuer | Entladen"
            )
        )

    # ========================================================
    # 3. GÜNSTIGSTE 8
    #    Laden hat Vorrang vor PV-Zeit.
    # ========================================================
    if rank < CHEAP_HIGH_COUNT:
        if current_soc < target_soc:
            return (
                "CHARGE",
                -CHARGE_POWER_HIGH,
                (
                    f"Preisrang {rank + 1}/{n_slots} | "
                    f"{price:.2f} ct/kWh | "
                    f"Ziel {target_soc:.0f}% | "
                    "Laden trotz PV"
                )
            )

        # NEU 3.9.6: in der PV-Zeit übernimmt der PV-Schutz
        if not forecast_pv_active():
            return (
                "HOLD",
                0,
                f"SOC-Ziel {target_soc:.0f}% erreicht"
            )

    # ========================================================
    # 4. RANG 9-16
    #    Laden hat Vorrang vor PV-Zeit.
    # ========================================================
    if rank < CHEAP_MEDIUM_COUNT:
        if current_soc < target_soc:
            return (
                "CHARGE",
                -CHARGE_POWER_MEDIUM,
                (
                    f"Preisrang {rank + 1}/{n_slots} | "
                    f"{price:.2f} ct/kWh | "
                    f"Ziel {target_soc:.0f}% | "
                    "Laden trotz PV"
                )
            )

        # NEU 3.9.6: in der PV-Zeit übernimmt der PV-Schutz
        if not forecast_pv_active():
            return (
                "HOLD",
                0,
                f"SOC-Ziel {target_soc:.0f}% erreicht"
            )

    # ========================================================
    # 5. PV-ZEIT AB RANG 17
    #
    # Rang 17-24: PV hat Vorrang; kein erzwungenes Netzladen.
    # Rang 25-72: bestehender PV-Schutz bleibt erhalten.
    # NEU 3.9.6: auch Rang 1-16, wenn das SOC-Ziel erreicht ist.
    # ========================================================
    if forecast_pv_active():
        protection = pv_protection_active
        balance = pv_balance()

        if rank < CHEAP_MEDIUM_COUNT:
            pv_note = f" | SOC-Ziel {target_soc:.0f}% erreicht"
        else:
            pv_note = ""

        if balance is None:
            return (
                "GENERAL",
                None,
                "PV-Zeit | Live-PV-Daten fehlen"
            )

        if protection:
            return (
                "HOLD",
                0,
                (
                    f"PV-Schutz | "
                    f"PV={current_pv_power:.0f}W | "
                    f"Haus={current_house_load:.0f}W | "
                    f"Bilanz={balance:.0f}W | "
                    f"Preisrang {rank + 1}/{n_slots}"
                    f"{pv_note}"
                )
            )

        return (
            "GENERAL",
            None,
            (
                f"PV-Zeit | "
                f"PV={current_pv_power:.0f}W | "
                f"Haus={current_house_load:.0f}W | "
                f"Bilanz={balance:+.0f}W | "
                f"Preisrang {rank + 1}/{n_slots} | "
                "PV bevorzugt | kein Netzladen"
                f"{pv_note}"
            )
        )

    # ========================================================
    # 6. RANG 17-24 AUSSERHALB PV-ZEIT
    # ========================================================
    if rank < CHEAP_MAX_COUNT:
        if current_soc < target_soc:
            return (
                "CHARGE",
                -CHARGE_POWER_LOW,
                (
                    f"Preisrang {rank + 1}/{n_slots} | "
                    f"{price:.2f} ct/kWh | "
                    f"Ziel {target_soc:.0f}%"
                )
            )

        return (
            "HOLD",
            0,
            f"SOC-Ziel {target_soc:.0f}% erreicht"
        )

    # ========================================================
    # 7. RANG 25-72 AUSSERHALB PV-ZEIT
    #    Vorausschauende Entscheidung.
    # ========================================================
    allow_discharge, reason = predictive_discharge_decision(
        price,
        rank,
        n_slots
    )

    if allow_discharge:
        return (
            "GENERAL",
            None,
            reason
        )

    return (
        "HOLD",
        0,
        reason
    )


# ============================================================
# PREISPLÄNE
# ============================================================

def ensure_today_plan():

    global last_today_attempt

    day_string = today_string()

    if get_day_plan(
        day_string
    ) is not None:

        return

    if (
        time.time()
        - last_today_attempt
        < 900
    ):

        return

    last_today_attempt = time.time()

    log(
        f"HEUTE-Plan fehlt. "
        f"Abruf {day_string}..."
    )

    entries = fetch_day(
        today_date()
    )

    if entries is not None:

        store_day_plan(
            day_string,
            entries
        )


def ensure_tomorrow_plan():

    global last_day_ahead_attempt

    day_string = tomorrow_string()

    if get_day_plan(
        day_string
    ) is not None:

        return

    clock = now_berlin().time()

    if clock < dt_time(12, 30):
        return

    elapsed = (
        time.time()
        - last_day_ahead_attempt
    )

    if clock < dt_time(14, 0):

        if elapsed < 900:
            return

    else:

        if elapsed < 3600:
            return

    last_day_ahead_attempt = (
        time.time()
    )

    log(
        f"MORGEN-Plan Abruf: "
        f"{day_string}"
    )

    entries = fetch_day(
        tomorrow_date()
    )

    if entries is not None:

        store_day_plan(
            day_string,
            entries
        )


# ============================================================
# NEU 3.9.5: PREISINFO VERÖFFENTLICHEN
#
# Für Shelly-Plugs (z. B. Waschmaschinen).
# Retained, damit ein Plug nach Neustart sofort den
# aktuellen Stand bekommt.
# ============================================================

def publish_price_info():

    global last_price_publish
    global last_published_slot

    if mqtt_client is None:
        return

    if not mqtt_client.is_connected():
        return

    now = time.time()

    rank_info = current_price_rank()

    if rank_info is None:

        slot_key = None

        payload = {
            "ok": False,
            "reason": "kein HEUTE-Plan",
            "valid_until": int(now) + 2 * PRICE_PUBLISH_SECONDS
        }

    else:

        rank, price, slot, n_slots = rank_info

        slot_start = int(slot.timestamp())

        slot_key = slot_start

        payload = {
            "ok": True,
            "rank": rank + 1,
            "n": n_slots,
            "price_ct": round(price, 2),
            "slot_start": slot_start,
            "valid_until": slot_start + SLOT_SECONDS
        }

    if (
        slot_key == last_published_slot
        and
        now - last_price_publish
        < PRICE_PUBLISH_SECONDS
    ):
        return

    try:

        mqtt_client.publish(
            TOPIC_PRICE_INFO,
            json.dumps(payload),
            qos=0,
            retain=True
        )

        if slot_key != last_published_slot:

            if payload["ok"]:

                log(
                    f"Preisinfo -> {TOPIC_PRICE_INFO} | "
                    f"Rang {payload['rank']}/{payload['n']} | "
                    f"{payload['price_ct']:.2f} ct/kWh"
                )

            else:

                log(
                    f"Preisinfo -> {TOPIC_PRICE_INFO} | "
                    f"ok=false | {payload['reason']}"
                )

        last_price_publish = now
        last_published_slot = slot_key

    except Exception as e:

        log(
            f"Preisinfo Fehler: {e}"
        )


# ============================================================
# HINTERGRUND
# ============================================================

def price_worker():

    log(
        "EPEX/PV Hintergrund-Timer gestartet"
    )

    with price_lock:

        cleanup_price_days()

        ensure_today_plan()

        ensure_tomorrow_plan()

        get_current_price(
            force=True
        )

        ensure_pv_forecast()

    while True:

        try:

            with price_lock:

                cleanup_price_days()

                ensure_today_plan()

                ensure_tomorrow_plan()

                get_current_price()

                ensure_pv_forecast()

                publish_price_info()

        except Exception as e:

            log(
                f"Hintergrundfehler: {e}"
            )

        time.sleep(
            PRICE_THREAD_SLEEP_SECONDS
        )


# ============================================================
# AUTOMATIK
# ============================================================

def decide_control():

    if not AUTO_CONTROL:

        return (
            "GENERAL",
            None,
            "Automatik AUS"
        )

    if not measurements_valid():

        return (
            "GENERAL",
            None,
            "Messdaten fehlen"
        )

    # NEU 3.9.7: Der Tagesplan reicht. Der Einzelpreis
    # wird bei Bedarf aus dem Plan ergänzt.

    if get_day_plan(
        today_string()
    ) is None:

        return (
            "GENERAL",
            None,
            "Kein HEUTE-Tagesplan"
        )

    ensure_price_value()

    if current_price_ct is None:

        return (
            "GENERAL",
            None,
            "Kein aktueller Preis"
        )

    return desired_control()


def ensure_price_value():

    # NEU 3.9.7: Solange Energy-Charts keinen Einzelpreis
    # liefert, gilt der Planpreis der laufenden
    # Viertelstunde (wird jede Viertelstunde mitgeführt).

    global current_price_ct
    global current_price_source

    if current_price_source == "live":
        return

    fallback = plan_price_now()

    if fallback is None:
        return

    if current_price_source != "plan":
        log(
            "EPEX aktuell: Ersatz aus Tagesplan"
        )

    current_price_ct = fallback
    current_price_source = "plan"


# ============================================================
# MQTT STEUERUNG
# ============================================================

def send_control(
    client,
    mode,
    power,
    reason
):

    global last_command_time
    global last_command_power
    global controller_has_control
    global last_control_mode

    now = time.time()


    # ========================================================
    # GENERAL
    # ========================================================

    if mode == "GENERAL":

        if controller_has_control:

            client.publish(
                TOPIC_EMS_MODE,
                "general",
                qos=0,
                retain=False
            )

            log(
                f"EMS -> general | "
                f"Hoymiles übernimmt | "
                f"{reason}"
            )

        controller_has_control = False
        last_command_power = None
        last_command_time = now

        if last_control_mode != "GENERAL":

            log(
                f"AUTO -> GENERAL | {reason}"
            )

        last_control_mode = "GENERAL"

        return


    # ========================================================
    # HOLD
    # ========================================================

    if mode == "HOLD":

        if not controller_has_control:

            client.publish(
                TOPIC_EMS_MODE,
                "mqtt_ctrl",
                qos=0,
                retain=False
            )

            log(
                f"EMS -> mqtt_ctrl | "
                f"HOLD | {reason}"
            )

            time.sleep(0.5)

            last_command_power = None

        if (
            last_command_power is None
            or
            now - last_command_time
            >= HOLD_INTERVAL_SECONDS
        ):

            if last_command_power == 0:

                hold_power = 0.1

            else:

                hold_power = 0

            client.publish(
                TOPIC_POWER,
                str(hold_power),
                qos=0,
                retain=False
            )

            last_command_power = hold_power
            last_command_time = now

        controller_has_control = True

        if last_control_mode != "HOLD":

            log(
                f"AUTO -> HOLD | {reason}"
            )

        last_control_mode = "HOLD"

        return


    # ========================================================
    # CHARGE
    # ========================================================

    if mode == "CHARGE":

        if power is None:

            log(
                "SICHERHEIT: "
                "CHARGE ohne Leistungswert verworfen"
            )

            return

        if power > 0:

            log(
                "SICHERHEIT: "
                "positiver Ladebefehl verworfen"
            )

            return

        power = max(
            -MAX_CHARGE_POWER_W,
            power
        )

        if not controller_has_control:

            client.publish(
                TOPIC_EMS_MODE,
                "mqtt_ctrl",
                qos=0,
                retain=False
            )

            log(
                f"EMS -> mqtt_ctrl | "
                f"EPEX-Laden | {reason}"
            )

            time.sleep(0.5)

            last_command_power = None

        if (
            last_command_power != power
            or
            now - last_command_time
            >= COMMAND_INTERVAL_SECONDS
        ):

            client.publish(
                TOPIC_POWER,
                str(int(power)),
                qos=0,
                retain=False
            )

            log(
                f"AUTO LADEN: "
                f"{abs(power)} W | "
                f"SOC={current_soc:.1f}% | "
                f"EPEX={current_price_ct:.2f} | "
                f"{reason}"
            )

            last_command_power = power
            last_command_time = now

        controller_has_control = True

        if last_control_mode != "CHARGE":

            log(
                f"AUTO -> CHARGE | {reason}"
            )

        last_control_mode = "CHARGE"


# ============================================================
# MQTT CALLBACKS
# ============================================================

def on_connect(
    client,
    userdata,
    flags,
    reason_code,
    properties
):

    # NEU 3.9.6: abgelehnte Anmeldung klar melden
    if reason_code.is_failure:

        log(
            f"MQTT abgelehnt: {reason_code} | "
            "Benutzer/Passwort in config.json prüfen"
        )

        return

    log(
        f"MQTT verbunden: "
        f"{reason_code}"
    )

    client.subscribe(
        TOPIC_STATE
    )

    log(
        f"Überwache: "
        f"{TOPIC_STATE}"
    )


def on_message(
    client,
    userdata,
    msg
):

    global current_soc
    global current_status

    global current_battery_power
    global current_house_load
    global current_grid_power
    global current_pv_power

    global last_state_received

    try:

        data = json.loads(
            msg.payload.decode(
                "utf-8"
            )
        )


        soc = data.get("soc")

        if soc is not None:

            current_soc = float(soc)


        current_status = data.get(
            "bat_sts"
        )


        battery = data.get(
            "bat_p"
        )

        if battery is not None:

            current_battery_power = float(
                battery
            )


        house = data.get(
            "sys_load_p"
        )

        if house is not None:

            current_house_load = float(
                house
            )

            if (
                0
                <= current_house_load
                <= 10000
            ):

                LOAD_HISTORY.append(
                    current_house_load
                )


        grid = data.get(
            "sys_grid_p"
        )

        if grid is not None:

            current_grid_power = float(
                grid
            )


        pv = data.get(
            "sys_pv_p"
        )

        if pv is not None:

            current_pv_power = float(
                pv
            )


        last_state_received = (
            time.time()
        )


        mode, power, reason = (
            decide_control()
        )


        send_control(
            client,
            mode,
            power,
            reason
        )


        avg_load = average_house_load()

        if avg_load is None:
            avg_text = "?"
        else:
            avg_text = (
                f"{avg_load:.0f}W"
            )


        if current_price_ct is None:
            price_text = "?"
        else:
            price_text = (
                f"{current_price_ct:.2f}"
            )


        radiation = (
            current_forecast_radiation()
        )


        balance = pv_balance()

        if balance is None:
            balance_text = "?"
        else:
            balance_text = (
                f"{balance:+.0f}W"
            )


        pv_text = (
            "?"
            if current_pv_power is None
            else f"{current_pv_power:.1f}W"
        )

        house_text = (
            "?"
            if current_house_load is None
            else f"{current_house_load:.1f}W"
        )

        battery_text = (
            "?"
            if current_battery_power is None
            else f"{current_battery_power:.1f}W"
        )

        grid_text = (
            "?"
            if current_grid_power is None
            else f"{current_grid_power:.1f}W"
        )


        print(
            f"{now_berlin().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"SOC={current_soc:.1f}% | "
            f"Status={current_status} | "
            f"Batterie={battery_text} | "
            f"PV={pv_text} | "
            f"Haus={house_text} | "
            f"Bilanz={balance_text} | "
            f"ØHaus={avg_text} | "
            f"Netz={grid_text} | "
            f"EPEX={price_text} | "
            f"GTI={radiation:.0f}W/m² | "
            f"PV-Schutz={pv_protection_active} | "
            f"AUTO={mode} | "
            f"{reason}",
            flush=True
        )


    except Exception as e:

        log(
            f"MQTT-Datenfehler: {e}"
        )


# ============================================================
# NEU 3.9.4: SIGTERM
#
# docker stop / Neustart sendet SIGTERM.
# Wird wie Strg+C behandelt -> EMS -> general.
# ============================================================

def handle_sigterm(signum, frame):

    raise KeyboardInterrupt


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    signal.signal(
        signal.SIGTERM,
        handle_sigterm
    )

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="hibattery-controller"
    )

    client.on_connect = on_connect
    client.on_message = on_message

    # NEU 3.9.6: Anmeldung am Broker
    if MQTT_USERNAME:
        client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD
        )

    mqtt_client = client


    log("")
    log(
        f"HiBattery Controller {VERSION} startet"
    )

    # NEU 3.9.6
    for message in CONFIG_MESSAGES:
        log(message)

    log(
        f"MQTT: {BROKER}:{PORT} | "
        + (
            f"Benutzer {MQTT_USERNAME}"
            if MQTT_USERNAME
            else "ohne Anmeldung"
        )
    )

    log(
        f"Gerät: {MASTER}"
    )

    log(
        "Zeitzone: Europe/Berlin"
    )

    log(
        f"AUTO_CONTROL = {AUTO_CONTROL}"
    )

    log(
        f"Speicher: "
        f"{BATTERY_CAPACITY_KWH:.1f} kWh"
    )

    log(
        f"SOC Minimum: "
        f"{SOC_MIN:.0f}%"
    )

    log(
        f"Typische Hauslast: "
        f"{TYPICAL_HOUSE_LOAD_W} W"
    )

    log(
        f"Reserveaufschlag: "
        f"{(HOUSE_RESERVE_FACTOR - 1) * 100:.0f}%"
    )

    log(
        f"Planungshorizont: "
        f"{LOOKAHEAD_HOURS} Stunden"
    )

    log(
        f"Minimaler Preisvorteil: "
        f"{MIN_PRICE_ADVANTAGE_CT:.1f} ct/kWh"
    )

    log(
        f"Viertelstunden heute: "
        f"{expected_slots(today_date())} | "
        f"morgen: "
        f"{expected_slots(tomorrow_date())}"
    )

    log("")

    log(
        "PV-Anlage:"
    )

    log(
        f"  Standort {PV_LATITUDE:.3f}, "
        f"{PV_LONGITUDE:.3f} | "
        f"{PV_POWER_KWP:.1f} kWp"
    )

    log(
        f"  Azimut {PV_AZIMUTH}° | "
        f"Neigung {PV_TILT}°"
    )

    log("")

    log(
        "PV-Ladeziele:"
    )

    log(
        f"  GUT >= {PV_GOOD_KWH:.1f} kWh "
        f"-> {SOC_TARGET_PV_GOOD:.0f}%"
    )

    log(
        f"  MITTEL "
        f"{PV_MEDIUM_KWH:.1f}-"
        f"{PV_GOOD_KWH:.1f} kWh "
        f"-> {SOC_TARGET_PV_MEDIUM:.0f}%"
    )

    log(
        f"  SCHLECHT < {PV_MEDIUM_KWH:.1f} kWh "
        f"-> {SOC_TARGET_PV_BAD:.0f}%"
    )

    log("")

    log(
        "Preisstrategie:"
    )

    log(
        f"  Rang 01-{CHEAP_HIGH_COUNT:02d} -> "
        f"CHARGE {CHARGE_POWER_HIGH} W"
    )

    log(
        f"  Rang {CHEAP_HIGH_COUNT + 1:02d}-"
        f"{CHEAP_MEDIUM_COUNT:02d} -> "
        f"CHARGE {CHARGE_POWER_MEDIUM} W"
    )

    log(
        f"  Rang {CHEAP_MEDIUM_COUNT + 1:02d}-"
        f"{CHEAP_MAX_COUNT:02d} -> "
        f"CHARGE {CHARGE_POWER_LOW} W"
    )

    log(
        f"  Rang {CHEAP_MAX_COUNT + 1:02d}-"
        f"{96 - EXPENSIVE_COUNT:02d} -> "
        "VORAUSSCHAU / GENERAL oder HOLD"
    )

    log(
        f"  teuerste {EXPENSIVE_COUNT} -> "
        "GENERAL / Entladen"
    )

    log("")

    log(
        "PV-Regel:"
    )

    log(
        f"  GTI >= "
        f"{PV_RADIATION_ACTIVE_WM2} W/m² "
        f"-> PV-Zeit (laufende Stunde)"
    )

    log(
        f"  PV-Zeit ab Rang {CHEAP_MEDIUM_COUNT + 1} -> "
        "kein erzwungenes Netzladen"
    )

    log(
        f"  PV-Zeit Rang 1-{CHEAP_MEDIUM_COUNT}, "
        "Ziel erreicht -> PV-Schutz (NEU 3.9.6)"
    )

    log(
        f"  Unterdeckung > "
        f"{PV_POWER_MARGIN_W} W "
        f"für {PV_DEFICIT_DELAY_SECONDS}s "
        f"-> HOLD"
    )

    log(
        f"  Überschuss > "
        f"{PV_POWER_MARGIN_W} W "
        f"für {PV_SURPLUS_DELAY_SECONDS}s "
        f"-> GENERAL"
    )

    log(
        "  Keine PV-Zeit -> "
        "PV-Schutz=False"
    )

    log("")

    log(
        f"Preisdatei: {PRICE_FILE}"
    )

    log(
        f"Preisinfo für Shelly: {TOPIC_PRICE_INFO} "
        f"(retained, alle {PRICE_PUBLISH_SECONDS}s)"
    )

    log(
        f"LIVE-AUTOMATIK {VERSION} AKTIV"
    )


    load_price_file()


    price_thread = threading.Thread(
        target=price_worker,
        daemon=True,
        name="epex-pv-worker"
    )

    price_thread.start()


    # ========================================================
    # MQTT HAUPTSCHLEIFE
    # ========================================================

    while True:

        try:

            client.connect(
                BROKER,
                PORT,
                60
            )

            client.loop_forever()


        except KeyboardInterrupt:

            if controller_has_control:

                try:

                    client.publish(
                        TOPIC_EMS_MODE,
                        "general",
                        qos=0,
                        retain=False
                    )

                    # Netzwerk kurz bedienen, damit der
                    # Befehl sicher rausgeht.
                    client.loop(timeout=0.5)

                    time.sleep(0.5)

                    log(
                        "Controller beendet | "
                        "EMS -> general"
                    )

                except Exception:
                    pass

            try:
                client.disconnect()
            except Exception:
                pass

            log(
                "Controller beendet"
            )

            break


        except Exception as e:

            log(
                f"MQTT-Verbindung Fehler: {e}"
            )

            log(
                "Neuer Versuch in 10 Sekunden"
            )

            time.sleep(10)
