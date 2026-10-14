# HiBattery Controller

Preis- und PV-gesteuerte Lade- und Entladesteuerung für Hoymiles-Akkuspeicher (z. B. MS-A2) über MQTT.

Der Controller lädt den Akku in den günstigsten Viertelstunden des Tages aus dem Netz, entlädt ihn in den teuersten und lässt tagsüber der eigenen PV-Anlage den Vortritt. Grundlage sind die Day-Ahead-Börsenpreise (EPEX Spot, Gebotszone DE-LU) und eine Wetterprognose für den Standort der PV-Module.

Optional veröffentlicht der Controller den aktuellen Preisrang per MQTT, damit z. B. Shelly-Steckdosen große Verbraucher (Waschmaschine, Trockner) nur in günstigen Zeiten freigeben.

Passend dazu gibt es ein [Display-Projekt](#passendes-projekt-display) zur Anzeige der Werte.

> **Hinweis:** Privates Hobbyprojekt, kein offizielles Produkt von Hoymiles oder Shelly. Nutzung auf eigene Verantwortung. Der Controller greift direkt in die Betriebsart deines Akkus ein.

---

## Inhalt

| Datei | Zweck |
|---|---|
| `controller.py` | Der Controller (Python 3) |
| `config.example.json` | Beispielkonfiguration, als `config.json` kopieren und anpassen |
| `docker-compose.example.yml` | Beispiel für den Betrieb als Docker-Container |
| `mosquitto/mosquitto.conf.example` | Beispielkonfiguration für den MQTT-Broker |
| `shelly/shelly_plug.js` | Optionales Script für Shelly Plus Plug S |
| `LICENSE` | Lizenz (MIT) |

---

## Voraussetzungen

**Pflicht**

- **Hoymiles-Akku mit lokaler MQTT-Schnittstelle**, z. B. MS-A2. In der Hoymiles-App (S-Miles Home) muss MQTT aktiviert und auf deinen eigenen Broker eingestellt sein. Der Akku veröffentlicht dann im Home-Assistant-Format unter anderem
  - `homeassistant/sensor/<GERÄTE-ID>/quick/state` (SOC, Akku-, Haus-, Netz- und PV-Leistung)
  - und nimmt Befehle an über `homeassistant/select/<GERÄTE-ID>/ems_mode/command` und `homeassistant/number/<GERÄTE-ID>/power_ctrl/set`.
- **MQTT-Broker**, z. B. [Eclipse Mosquitto](https://mosquitto.org/) ab 2.0, im lokalen Netz.
- **Ein Rechner, der dauerhaft läuft**: NAS, Raspberry Pi, Mini-PC. Am einfachsten mit Docker (z. B. QNAP Container Station, Synology Container Manager, Portainer).
- **Python 3.9 oder neuer** und das Paket `paho-mqtt` ab Version 2.0 (im Docker-Beispiel automatisch).
- **Internetzugang** für den Rechner, um Börsenpreise und Wetterprognose abzurufen.

**Optional**

- **PV-Anlage / Balkonkraftwerk**, deren Leistung der Akku mitmisst. Ohne PV setzt du `kwp` klein und die PV-Logik spielt kaum eine Rolle.
- **Shelly Plus Plug S** (Gen2 oder neuer) für preisgesteuerte Verbraucher.
- **Dynamischer Stromtarif.** Der Controller arbeitet mit dem Börsenpreis. Mit einem dynamischen Tarif sparst du direkt Geld, mit einem festen Tarif lohnt sich vor allem die PV-Logik.

**Verwendete Online-Dienste** (kostenlos, ohne Anmeldung)

- [Energy-Charts](https://api.energy-charts.info/) (Fraunhofer ISE) für die Day-Ahead-Preise
- [Open-Meteo](https://open-meteo.com/) für die Einstrahlungsprognose auf die Modulfläche

---

## Installation

### 1. MQTT-Broker einrichten

Falls du noch keinen Broker hast, starte Mosquitto (z. B. als Container `eclipse-mosquitto`) mit der Beispieldatei `mosquitto/mosquitto.conf.example` als `mosquitto.conf`.

Mosquitto 2.x lässt ohne eigene Konfiguration nur Verbindungen von `localhost` zu. Die Zeile `listener 1883` ist deshalb nötig.

### 2. Akku mit dem Broker verbinden

In der Hoymiles-App bei den MQTT-Einstellungen des Akkus die IP-Adresse deines Brokers und Port `1883` eintragen.

Prüfen, ob Daten ankommen, und dabei die **Geräte-ID** herausfinden:

```sh
mosquitto_sub -h <IP-des-Brokers> -t 'homeassistant/sensor/#' -v
```

Du siehst Zeilen wie `homeassistant/sensor/MSA-123456789012/quick/state {"soc": 54, ...}`. Der Teil zwischen `sensor/` und `/quick` ist die Geräte-ID, die in die Konfiguration gehört.

### 3. Controller-Dateien ablegen

Lege einen Ordner an, z. B. `hibattery/`, und kopiere hinein:

- `controller.py`
- `config.example.json` als **`config.json`**

In diesen Ordner schreibt der Controller auch sein Log (`hibattery.log`) und die zwischengespeicherten Preise (`epex_prices.json`).

### 4. Konfiguration anpassen

Mindestens diese Werte in der `config.json` musst du ändern:

| Abschnitt | Schlüssel | Bedeutung |
|---|---|---|
| `mqtt` | `broker` | IP-Adresse deines MQTT-Brokers |
| `geraet` | `master` | Geräte-ID des Akkus aus Schritt 2 |
| `speicher` | `kapazitaet_kwh` | Nutzbare Kapazität des Akkus |
| `pv_anlage` | `breitengrad`, `laengengrad` | Standort der Module (zwei bis drei Nachkommastellen genügen) |
| `pv_anlage` | `kwp`, `azimut`, `neigung` | Leistung und Ausrichtung der Module |

Alle weiteren Werte sind unter [Konfiguration](#konfiguration) beschrieben.

### 5. Starten

**Mit Docker Compose**

`docker-compose.example.yml` als `docker-compose.yml` in den Ordner legen und starten:

```sh
docker compose up -d
docker compose logs -f
```

**Mit QNAP Container Station oder ähnlicher Oberfläche**

- Image: `python:3.12-slim`
- Ordner aus Schritt 3 als Volume nach `/app` einbinden
- Entrypoint: `sh`
- Command: `-c "pip install --no-cache-dir 'paho-mqtt>=2.0' tzdata && exec python /app/controller.py"`
- Neustart-Richtlinie: immer bzw. `unless-stopped`

Das `exec` ist wichtig: Nur so erhält der Controller beim Stoppen des Containers das Signal und gibt die Steuerung sauber an den Akku zurück.

**Ohne Docker**

Der Controller erwartet seine Dateien unter `/app`. Wenn du ihn direkt startest, passe in `controller.py` die Pfade `LOG_FILE`, `PRICE_FILE` und `CONFIG_FILE` an, dann:

```sh
pip install 'paho-mqtt>=2.0' tzdata
python3 controller.py
```

### 6. Prüfen

Im `hibattery.log` sollte nach dem Start stehen:

```
HiBattery Controller 3.9.7 startet
Config geladen: /app/config.json | 35 Werte
MQTT: 192.168.1.10:1883 | ohne Anmeldung
MQTT verbunden: Success
```

Kurz danach kommen die Tagespreise (`EPEX Tagesabruf`), die PV-Prognose und im laufenden Betrieb eine Zeile pro Entscheidung, z. B. `AUTO -> CHARGE`, `AUTO -> HOLD` oder `AUTO -> GENERAL` mit Begründung.

Steht dort `Keine /app/config.json`, liegt die Datei im falschen Ordner. Der Controller läuft dann mit den Standardwerten aus dem Code, die für deine Anlage nicht passen.

Beim Stoppen des Containers müssen die letzten Zeilen `Controller beendet | EMS -> general` und `Controller beendet` sein.

---

## Konfiguration

Alle Werte stehen in der `config.json`. Fehlt die Datei oder ein Wert, gilt der Standardwert aus `controller.py`. Ungültige Werte werden im Log gemeldet (`Config: ... ungültig`) und ignoriert. Einträge, deren Name mit `_` beginnt, sind Kommentare.

Nach jeder Änderung den Controller neu starten.

### `mqtt`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `broker` | `192.168.1.10` | IP-Adresse oder Name des Brokers |
| `port` | `1883` | Port |
| `username`, `password` | leer | Zugangsdaten. Leer = ohne Anmeldung. Das Passwort wird nie ins Log geschrieben. |

### `geraet`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `master` | `MSA-000000000000` | Geräte-ID des Akkus, der gesteuert wird |
| `auto_control` | `true` | `false` = nur beobachten. Der Akku bleibt in seiner eigenen Regelung (GENERAL), das Log zeigt trotzdem Preise und Prognosen. |

### `speicher`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `kapazitaet_kwh` | `10.0` | Nutzbare Kapazität |
| `soc_min` | `11.0` | Unter diesem Ladestand wird nicht weiter entladen |
| `soc_ziel_fallback` | `85.0` | Ladeziel, wenn keine PV-Prognose vorliegt |

### `haus`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `typische_last_w` | `350` | Grundlast des Hauses, solange noch keine Messwerte vorliegen |
| `reserve_faktor` | `1.15` | Aufschlag auf den berechneten Bedarf (Verluste, Schwankungen) |

### `vorausschau`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `stunden` | `24` | Wie weit der Controller bei mittleren Preisen vorausplant |
| `min_preisvorteil_ct` | `5.0` | So viel günstiger (ct/kWh) muss ein späteres Ladefenster sein, damit sich Entladen jetzt lohnt |

### `pv_anlage`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `kwp` | `0.8` | Modulleistung in kWp |
| `breitengrad`, `laengengrad` | `51.0`, `10.0` | Standort |
| `azimut` | `0` | Ausrichtung: 0 = Süd, -90 = Ost, +90 = West |
| `neigung` | `30` | Neigung gegenüber der Waagerechten: 0 = flach, 90 = senkrecht |
| `systemfaktor` | `0.85` | Verluste von Wechselrichter, Verschattung, Temperatur |

### `pv_ladeziel`

Aus der Prognose wird der erwartete PV-Ertrag des Tages berechnet. Je weniger Sonne erwartet wird, desto voller lädt der Controller den Akku in günstigen Zeiten aus dem Netz.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `gut_ab_kwh` | `2.5` | Ab diesem Tagesertrag gilt der Tag als gut |
| `mittel_ab_kwh` | `1.2` | Ab diesem Tagesertrag gilt der Tag als mittel |
| `ziel_gut` | `75.0` | Ladeziel in % bei guter Prognose |
| `ziel_mittel` | `85.0` | Ladeziel in % bei mittlerer Prognose |
| `ziel_schlecht` | `95.0` | Ladeziel in % bei schlechter Prognose |

### `ladeleistung`

Netzladeleistung in Watt, je nachdem wie günstig die Viertelstunde ist.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `hoch_w` | `1500` | In den günstigsten Viertelstunden |
| `mittel_w` | `1000` | In den nächstgünstigen |
| `niedrig_w` | `600` | In den übrigen günstigen |

### `preislogik`

Jede Viertelstunde des Tages bekommt einen Rang nach Preis, 1 ist die günstigste. Ein normaler Tag hat 96 Viertelstunden, bei der Zeitumstellung 92 oder 100.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `guenstig_hoch_anzahl` | `8` | Rang 1–8: Laden mit hoher Leistung |
| `guenstig_mittel_anzahl` | `16` | Rang 9–16: Laden mit mittlerer Leistung |
| `guenstig_max_anzahl` | `24` | Rang 17–24: Laden mit niedriger Leistung, aber nur außerhalb der PV-Zeit |
| `teuer_anzahl` | `24` | Die teuersten 24 Viertelstunden: Entladen |

Die Anzahlen müssen aufsteigen (hoch ≤ mittel ≤ max) und zusammen mit `teuer_anzahl` unter 96 bleiben, sonst gelten die Standardwerte.

### `pv_regelung`

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `strahlung_aktiv_wm2` | `40` | Ab dieser prognostizierten Einstrahlung (W/m²) gilt die Stunde als PV-Zeit |
| `leistungsmarge_w` | `30` | Toleranz beim Vergleich PV-Leistung gegen Hauslast |
| `unterdeckung_s` | `60` | So lange muss die PV die Hauslast unterschreiten, bevor der Akku gesperrt wird |
| `ueberschuss_s` | `30` | So lange muss wieder Überschuss da sein, bevor der Akku freigegeben wird |

---

## Funktionsweise

### Drei Betriebsarten

Der Controller entscheidet etwa alle 10 Sekunden neu und schaltet den Akku in eine von drei Betriebsarten:

- **GENERAL**: Der Akku regelt selbst (Eigenverbrauch). Er nimmt PV-Überschuss auf und deckt die Hauslast. Das ist auch der sichere Rückfall bei jedem Problem.
- **CHARGE**: Der Controller übernimmt (`mqtt_ctrl`) und lädt mit fester Leistung aus dem Netz.
- **HOLD**: Der Controller übernimmt und hält den Akku auf 0 W. Er wird weder geladen noch entladen, der Strom für das Haus kommt aus dem Netz.

### Entscheidungsreihenfolge

1. **SOC-Minimum erreicht**: In günstigen Viertelstunden laden, sonst HOLD. Entladen ist gesperrt.
2. **Teuerste 24 Viertelstunden**: GENERAL, der Akku entlädt sich ins Haus.
3. **Rang 1–16**: Laden bis zum Ladeziel, auch während der PV-Zeit. Ist das Ziel erreicht, gilt außerhalb der PV-Zeit HOLD, in der PV-Zeit die PV-Regelung (Punkt 4).
4. **PV-Zeit**: Solange die PV mehr liefert als das Haus braucht, GENERAL, damit der Überschuss in den Akku geht. Liegt die PV länger als `unterdeckung_s` unter der Hauslast, HOLD (PV-Schutz), damit der Akku nicht bei mittlerem Preis leer läuft. Bei erneutem Überschuss nach `ueberschuss_s` wieder GENERAL.
5. **Rang 17–24 außerhalb der PV-Zeit**: Laden mit niedriger Leistung bis zum Ladeziel, sonst HOLD.
6. **Mittlere Preise außerhalb der PV-Zeit**: Vorausschau über die nächsten 24 Stunden. Reicht der Akkustand für den erwarteten Hausverbrauch bis zum nächsten deutlich günstigeren Ladefenster, wird entladen (GENERAL). Sonst HOLD, damit der Strom für die teuren Stunden aufgehoben wird.

### Sicherheit

- Fehlen Messdaten vom Akku länger als 10 Sekunden, fehlt der Preisplan für heute oder ist `auto_control` aus, geht der Controller auf GENERAL. Der Akku arbeitet dann wie ohne Controller.
- Beim Beenden (Strg+C oder Stoppen des Containers) gibt der Controller die Steuerung mit `ems_mode = general` an den Akku zurück.
- Die Preise werden in `epex_prices.json` zwischengespeichert. Nach einem Neustart steht der Tagesplan sofort wieder zur Verfügung.
- Der Plan für morgen wird ab 12:30 Uhr abgerufen, nach Fehlern mit Wartezeit, um die API nicht zu überlasten.

### Preisinfo für andere Geräte

Alle 60 Sekunden und bei jedem Viertelstunden-Wechsel veröffentlicht der Controller auf `hibattery/price` (retained):

```json
{"ok": true, "rank": 5, "n": 96, "price_ct": 21.34,
 "slot_start": 1767225600, "valid_until": 1767226500}
```

`rank` 1 ist die günstigste Viertelstunde des Tages. Ohne gültigen Tagesplan wird `{"ok": false, ...}` gesendet. Jedes Gerät, das MQTT lesen kann, kann damit eigene Entscheidungen treffen.

---

## Optional: Shelly Plus Plug S

`shelly/shelly_plug.js` schaltet eine Steckdose in den 12 günstigsten Viertelstunden des Tages ein (insgesamt 3 Stunden). Gedacht für Geräte, die man vorbereitet und die dann loslaufen, sobald Strom kommt, z. B. eine Waschmaschine mit Startzeitvorwahl oder eine, die nach Stromausfall weiterläuft.

- Läuft das Gerät noch, wenn die günstige Zeit endet, wird nicht sofort abgeschaltet. Erst wenn die Leistung länger unter 5 W liegt (2 Minuten, oder 10 Minuten wenn das Gerät gelaufen ist), geht die Steckdose aus. Spätestens nach 4 Stunden Nachlauf.
- Ohne aktuelle Preisinfo (Controller aus) schaltet das Script nicht automatisch ein. Von Hand per Taste geht es immer.

**Einrichtung**

1. Im Shelly-Webinterface unter *Settings → MQTT* MQTT aktivieren, Server `<IP-des-Brokers>:1883` eintragen, ggf. Benutzer und Passwort.
2. Unter *Scripts* ein neues Script anlegen, den Inhalt von `shelly_plug.js` einfügen, speichern, starten und *Run on startup* aktivieren.
3. Im Script-Log erscheint zur nächsten Viertelstunde `Preisinfo: ... Rang x/96`.

Die Anzahl der günstigen Viertelstunden und die Leistungsgrenzen stehen oben im Script unter `EINSTELLUNGEN`.

---

## Passendes Projekt: Display

Für die Anzeige von Ladestand, Leistung und Strompreis gibt es ein eigenes Display-Projekt, das die Daten per MQTT vom gleichen Broker liest:

**[<NAME DES DISPLAY-PROJEKTS>](https://github.com/<BENUTZERNAME>/<DISPLAY-REPO>)**

---

## MQTT mit Passwort absichern (empfohlen)

1. Im Mosquitto-Container eine Passwortdatei anlegen:
   ```sh
   mosquitto_passwd -c /mosquitto/config/passwd hibattery
   chown mosquitto:mosquitto /mosquitto/config/passwd
   chmod 0700 /mosquitto/config/passwd
   ```
   Das `-c` nur beim ersten Mal, es überschreibt die Datei. Mosquitto 2.1 startet nicht, wenn die Datei fehlt oder zu offene Rechte hat.
2. In der `mosquitto.conf` `password_file /mosquitto/config/passwd` eintragen, `allow_anonymous true` vorerst lassen, Mosquitto neu starten.
3. Benutzer und Passwort nacheinander bei allen Teilnehmern eintragen: Controller (`config.json`, Abschnitt `mqtt`), Akku (Hoymiles-App), Shellys und alle weiteren Geräte.
4. Erst wenn alles läuft, `allow_anonymous false` setzen und Mosquitto neu starten. Klemmt etwas, zurück auf `true`.

Den Port 1883 nie im Router nach außen freigeben.

---

## Fehlersuche

| Meldung im Log | Ursache |
|---|---|
| `Keine /app/config.json` | Datei fehlt oder liegt im falschen Ordner |
| `MQTT abgelehnt: Not authorized` | Benutzer oder Passwort in der `config.json` falsch |
| `Messdaten fehlen` | Vom Akku kommen keine Daten. Geräte-ID (`master`) und MQTT-Einstellung in der Hoymiles-App prüfen |
| `Kein HEUTE-Tagesplan` | Preisabruf fehlgeschlagen (Internet, API). Der Controller versucht es alle 15 Minuten erneut und steuert bis dahin nicht |
| `Config: ... ungültig` | Wert hat das falsche Format, es gilt der Standardwert |

---

## Lizenz

MIT, siehe [LICENSE](LICENSE).
