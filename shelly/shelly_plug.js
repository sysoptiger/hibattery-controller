/*
 * HiBattery Preissteuerung – Shelly Plus Plug S (Gen2/Gen3)
 * z. B. für eine Waschmaschine
 * Version 2.0 (ab HiBattery Controller 3.9.5)
 *
 * NEU 2.0:
 * - Keine eigenen Abrufe bei Energy-Charts mehr.
 *   Der Preisrang kommt vom HiBattery-Controller per MQTT:
 *   Topic "hibattery/price" (retained)
 *   -> Zeitumstellung (92/100 Viertelstunden) automatisch richtig
 *
 * - Sicherer Nachlauf, damit nicht mitten im Programm
 *   abgeschaltet wird:
 *     Maschine ist nicht gelaufen  -> AUS nach 2 Min. <= 5 W
 *     Maschine ist gelaufen        -> AUS nach 10 Min. <= 5 W
 *       (Einweich- und Pausenphasen ziehen teils nur 1-3 W)
 *   Sicherheitsabschaltung erst nach 4 Std. Nachlauf.
 *
 * - Keine/veraltete Preisdaten (Controller aus):
 *   Plug wird NICHT eingeschaltet. Eine laufende Maschine
 *   läuft normal zu Ende. Manuell per Taste einschalten
 *   geht weiterhin.
 *
 * Unverändert:
 * - die 12 günstigsten Viertelstunden des Tages -> EIN
 *   (12 x 15 Min. = 3 Std. täglich)
 * - Prüfung alle 30 Sekunden
 *
 * VORAUSSETZUNG am Shelly:
 *   Einstellungen -> MQTT aktivieren
 *   Server: <IP-des-Brokers>:1883
 */


// ==========================================
// EINSTELLUNGEN
// ==========================================

let CHEAP_INTERVALS = 12;

let PRICE_TOPIC = "hibattery/price";

let POWER_OFF_LIMIT = 5;          // Watt
let RUN_LIMIT = 20;               // Watt: ab hier "Maschine läuft"

let LOW_POWER_TIME_IDLE = 120;    // Sek.: Maschine nicht gelaufen
let LOW_POWER_TIME_RUN = 600;     // Sek.: Maschine war in Betrieb

let MAX_OVERRUN = 14400;          // 4 Stunden

let CHECK_INTERVAL = 30;          // Sekunden


// ==========================================
// INTERNE VARIABLEN
// ==========================================

let priceInfo = null;

let lastCheapState = null;

let overrunStarted = 0;
let lowPowerStarted = 0;

let machineRan = false;


// ==========================================
// ZEITFUNKTIONEN
// ==========================================

function nowSeconds() {

  return Math.floor(Date.now() / 1000);
}


function twoDigits(n) {

  if (n < 10) {
    return "0" + n;
  }

  return "" + n;
}


function clockText(ts) {

  let d = new Date(ts * 1000);

  return twoDigits(d.getHours()) +
         ":" +
         twoDigits(d.getMinutes());
}


// ==========================================
// SCHALTEN
// ==========================================

function switchOn() {

  Shelly.call(
    "Switch.Set",
    {
      id: 0,
      on: true
    },
    function(result, error_code, error_message) {

      if (error_code !== 0) {

        print(
          "Fehler beim Einschalten:",
          error_code,
          error_message
        );

      } else {

        print(">>> STECKDOSE EIN");
      }
    }
  );
}


function switchOff() {

  Shelly.call(
    "Switch.Set",
    {
      id: 0,
      on: false
    },
    function(result, error_code, error_message) {

      if (error_code !== 0) {

        print(
          "Fehler beim Ausschalten:",
          error_code,
          error_message
        );

      } else {

        print(">>> STECKDOSE AUS");
      }
    }
  );

  overrunStarted = 0;
  lowPowerStarted = 0;
  machineRan = false;
}


// ==========================================
// PREISINFO VOM CONTROLLER
// ==========================================

function onPriceMessage(topic, message) {

  try {

    let data = JSON.parse(message);

    if (
      typeof(data.valid_until) !== "number"
    ) {

      print("Preisinfo ohne valid_until");

      return;
    }

    let changed =
      priceInfo === null ||
      priceInfo.slot_start !== data.slot_start ||
      priceInfo.ok !== data.ok;

    priceInfo = data;

    if (changed) {

      if (data.ok) {

        print(
          "Preisinfo: " +
          clockText(data.slot_start) +
          " Uhr | Rang " +
          data.rank + "/" + data.n +
          " | " + data.price_ct +
          " ct/kWh"
        );

      } else {

        print(
          "Preisinfo: ok=false | " +
          data.reason
        );
      }
    }

  } catch (e) {

    print(
      "Preisinfo Fehler:",
      e
    );
  }
}


// ==========================================
// PRÜFEN, OB JETZT GÜNSTIGE ZEIT IST
// ==========================================

function isCheapNow() {

  if (priceInfo === null) {
    return false;
  }

  if (!priceInfo.ok) {
    return false;
  }

  // Veraltet (z.B. Controller aus) -> nicht günstig

  if (nowSeconds() >= priceInfo.valid_until) {
    return false;
  }

  return priceInfo.rank <= CHEAP_INTERVALS;
}


function priceDataValid() {

  return (
    priceInfo !== null &&
    priceInfo.ok &&
    nowSeconds() < priceInfo.valid_until
  );
}


// ==========================================
// NACHLAUF / LEISTUNGSPRÜFUNG
// ==========================================

function checkPowerForShutdown(status) {

  let now = nowSeconds();


  // Nachlauf beginnt

  if (overrunStarted === 0) {

    overrunStarted = now;

    lowPowerStarted = 0;

    print("");
    print("Keine günstige Zeit.");
    print("Warte, bis Verbraucher fertig ist.");
  }


  let overrun =
    now - overrunStarted;


  // ========================================
  // Sicherheitsabschaltung nach 4 Stunden
  // ========================================

  if (overrun >= MAX_OVERRUN) {

    print("Max. Nachlaufzeit erreicht -> AUS");

    switchOff();

    return;
  }


  let power = status.apower;


  if (
    power === undefined ||
    power === null
  ) {

    print("Leistung konnte nicht gelesen werden");

    return;
  }


  if (power > RUN_LIMIT) {

    machineRan = true;
  }


  print(
    "Leistung:",
    power,
    "W | Nachlauf:",
    Math.floor(overrun / 60),
    "Min. | gelaufen:",
    machineRan
  );


  // ========================================
  // Verbraucher arbeitet noch
  // ========================================

  if (power > POWER_OFF_LIMIT) {

    lowPowerStarted = 0;

    return;
  }


  // ========================================
  // Leistung <= 5 Watt
  // ========================================

  let waitTime = LOW_POWER_TIME_IDLE;

  if (machineRan) {

    waitTime = LOW_POWER_TIME_RUN;
  }


  if (lowPowerStarted === 0) {

    lowPowerStarted = now;

    print(
      "Leistung <= " + POWER_OFF_LIMIT +
      " W | Wartezeit " +
      Math.floor(waitTime / 60) +
      " Min. gestartet"
    );

    return;
  }


  let lowPowerDuration =
    now - lowPowerStarted;


  if (lowPowerDuration >= waitTime) {

    print("Verbraucher fertig -> AUS");

    switchOff();
  }
}


// ==========================================
// HAUPTSTEUERUNG
// ==========================================

function control() {

  let cheap = isCheapNow();


  if (cheap !== lastCheapState) {

    if (cheap) {

      print("Günstige Zeit beginnt");

    } else if (!priceDataValid()) {

      print(
        "Keine gültige Preisinfo vom Controller " +
        "-> kein automatisches Einschalten"
      );

    } else {

      print("Keine günstige Zeit");
    }

    lastCheapState = cheap;
  }


  Shelly.call(

    "Switch.GetStatus",

    {
      id: 0
    },

    function(status, error_code, error_message) {

      if (error_code !== 0) {

        print(
          "Statusfehler:",
          error_code,
          error_message
        );

        return;
      }


      // =====================================
      // GÜNSTIGES INTERVALL
      // =====================================

      if (cheap) {

        overrunStarted = 0;
        lowPowerStarted = 0;

        if (
          status.apower !== undefined &&
          status.apower !== null &&
          status.apower > RUN_LIMIT
        ) {

          machineRan = true;
        }

        if (!status.output) {

          print("Günstige Zeit -> EIN");

          switchOn();
        }

        return;
      }


      // =====================================
      // KEINE GÜNSTIGE ZEIT
      // =====================================

      if (!status.output) {

        overrunStarted = 0;
        lowPowerStarted = 0;
        machineRan = false;

        return;
      }


      // Plug ist noch EIN:
      // Leistung prüfen statt sofort ausschalten

      checkPowerForShutdown(status);
    }
  );
}


// ==========================================
// START
// ==========================================

print("");
print("========================");
print("HIBATTERY PREISSTEUERUNG 2.0");
print("========================");

print("Preisquelle: MQTT", PRICE_TOPIC);
print("Günstige Intervalle:", CHEAP_INTERVALS);
print("Gesamtzeit:", CHEAP_INTERVALS * 15, "Minuten");
print("Abschaltgrenze:", POWER_OFF_LIMIT, "W");
print("Wartezeit ohne Lauf:", LOW_POWER_TIME_IDLE, "Sek.");
print("Wartezeit nach Lauf:", LOW_POWER_TIME_RUN, "Sek.");
print("Max. Nachlauf:", MAX_OVERRUN / 3600, "Std.");

if (!MQTT.isConnected()) {

  print("ACHTUNG: MQTT (noch) nicht verbunden");
}

print("========================");
print("");


// Preisinfo vom Controller abonnieren

MQTT.subscribe(
  PRICE_TOPIC,
  onPriceMessage
);


// Steuerung alle 30 Sekunden

Timer.set(

  CHECK_INTERVAL * 1000,

  true,

  function() {

    control();
  }
);
