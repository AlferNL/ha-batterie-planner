#!/usr/bin/env python3
# planner.py - Batterie-Planer v2 (Home-Assistant-Add-on)
# ============================================================================
# Stuendlicher Batterie-Fahrplan (rolling horizon) fuer die Marstek-v2-Steuerung.
# Python-Port des Plan-Kerns aus batterie_schatten.ps1 (BrainWiki, Laptop);
# die abendliche Auswertung (Eval/Bericht) bleibt auf dem Laptop, dieses Add-on
# ist der EINZIGE Schreiber des Plans (brainwiki/batterie/v2plan, MQTT retained).
#
# Ablauf: jede Stunde um Minute :takt_minute wird der Rest des Tages neu geplant
# (Ist-SoC, Rest-Preise, frisches Solcast); um 23:takt zusaetzlich der Folgetag.
# Die laufende Stunde ist eingefroren (kein Umschalten mitten in der Stunde).
# Publiziert wird nur, wenn sich der Restplan wirklich aendert (Flatter-Bremse).
#
# Notbremse: jeder fertige Plan muss harte Invarianten bestehen (Entladung nie
# ueber 800 W, Ladung 250..2000 W, kein Handel auf gesperrten Preisstunden,
# SoC-Bahn in den Grenzen). Bei Verstoss wird NICHT publiziert, nur gemeldet -
# der letzte gueltige Plan bleibt retained stehen.
#
# Harte Vorgaben von Andre (aus v1/v2 uebernommen):
#   - Entladung NIE ueber 800 W (zusaetzlich gegen HA-Maximalwert gekappt).
#   - Echte NextEnergy-Preise: Einkauf = Beurs + Live-Offset, Verkauf =
#     Teruglever-Wertmodell live aus den HA-Helfern (Config-Flips statt Code).
#   - Kein Verlust-Trade: Gewinn-Ungleichung mit Wirkungsgrad + gate_puffer.
# ============================================================================

import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# ------------------------------------------------------------------ Konstanten
KAP_KWH = 5.12            # Marstek Venus E, nutzbare Nennkapazitaet
BODEN_PCT = 12.3          # E7-Boden: darunter gilt der Akku als leer
ENTL_MAX_W_HART = 800     # Andres harte Vorgabe: NIE mehr
LADEN_NETZ_MAX_W = 2000   # obere Klemme der Zwangsladung
LADEN_PV_MAX_W = 2500     # Hardware-Maximum Laden
MAX_ITER = 500            # Notbremse Optimierer

API = "http://supervisor/core/api"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DATA_DIR = "/data"
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LAST_PLAN_PATH = os.path.join(DATA_DIR, "last_plan.json")

PLAN_TOPIC_BASIS = "brainwiki/batterie/v2plan"
STATUS_TOPIC_BASIS = "brainwiki/batterie/v2planner"

ZAEHLER = {
    "imp": "sensor.p1_meter_energy_import",
    "exp": "sensor.p1_meter_energy_export",
    # Batterie-Zaehler des GERAETS (Modbus, ticken alle ~60 s in 0,01-kWh-
    # Schritten). Die HA-Integrationshelfer (batterij_ontladen_kwh,
    # batterij_laden_uit_net/pv_kwh) buchen bei konstanter Leistung erst am
    # Leistungswechsel in einem Schub (Befund 2026-09-03: 3,77 kWh
    # Nachtladung komplett in Stunde 5, 2,5 kWh Abendentladung in Stunde 22)
    # und verzerrten das Hausprofil um ganze kWh je Stunde; ein 3-Tage-Median
    # hielt dann 1,3 kWh "PV" um 5 Uhr morgens fuer real.
    "dis": "sensor.marstek_venus_modbus_gesamte_entladeenergie",
    "lad": "sensor.marstek_venus_modbus_gesamte_ladeenergie",
}
PROFIL_VERSION = 2  # Zaehlerquelle geaendert: gecachte Tage im State werden neu gerechnet

TZ = None  # wird beim Start aus der HA-Config gesetzt


# --------------------------------------------------------------------- Helfer
def log(text):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text, flush=True)


def jetzt():
    return datetime.now(TZ)


def api_call(pfad, methode="GET", body=None):
    daten = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + pfad, method=methode, data=daten,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as antwort:
        text = antwort.read()
        return json.loads(text) if text else None


def zustand(entity):
    return api_call("/states/" + entity)


def zahl(entity, fallback):
    try:
        return float(zustand(entity)["state"])
    except Exception:
        return fallback


def dienst(domain, service, daten):
    return api_call("/services/%s/%s" % (domain, service), "POST", daten)


def parse_iso(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(TZ)


def melde(titel, nachricht):
    # Feste notification_id: eine wiederholte Stoerung ERSETZT ihre Meldung,
    # statt sie stuendlich zu stapeln (Nacht 29./30.08.: 13 identische Meldungen).
    try:
        dienst("persistent_notification", "create",
               {"title": titel, "message": nachricht,
                "notification_id": "batterie_planner_v2"})
    except Exception as e:
        log("WARNUNG: Meldung fehlgeschlagen: %s" % e)


def lade_json(pfad, fallback):
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def schreibe_json(pfad, obj):
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, pfad)


# ------------------------------------------------------------------ HA-Historie
def serie(entity, von, bis):
    # Liste (zeit, wert) numerischer Zustaende, Zeit in lokaler Zone.
    pfad = ("/history/period/" + urllib.parse.quote(von.isoformat())
            + "?end_time=" + urllib.parse.quote(bis.isoformat())
            + "&filter_entity_id=" + entity
            + "&minimal_response&no_attributes&significant_changes_only=false")
    roh = api_call(pfad) or []
    if roh and isinstance(roh[0], list):
        roh = roh[0]
    punkte = []
    for e in roh:
        try:
            v = float(e["state"])
        except (ValueError, TypeError, KeyError):
            continue
        ts = e.get("last_changed") or e.get("last_updated")
        if not ts:
            continue
        try:
            punkte.append((parse_iso(ts), v))
        except ValueError:
            continue
    return punkte


def integriere_stunden(punkte, tag0):
    # Zeitgewichtete Integration einer W-Stufenkurve zu 24 Stunden-kWh.
    kwh = [0.0] * 24
    if not punkte:
        return kwh
    for h in range(24):
        von = tag0 + timedelta(hours=h)
        bis = tag0 + timedelta(hours=h + 1)
        wert = None
        for (t, v) in punkte:
            if t <= von:
                wert = v
            else:
                break
        t0 = von
        summe_wh = 0.0
        for (t, v) in punkte:
            if t <= von:
                continue
            if t >= bis:
                break
            if wert is not None:
                summe_wh += wert * (t - t0).total_seconds() / 3600.0
            t0 = t
            wert = v
        if wert is not None:
            summe_wh += wert * (bis - t0).total_seconds() / 3600.0
        kwh[h] = round(summe_wh / 1000.0, 4)
    return kwh


# ------------------------------------------------------------ Konfig und Preise
def lese_konfig():
    cfg = {}
    cfg["bonus_pct"] = zahl("input_number.ne_zonnebonus_percentage", 0) / 100.0
    cfg["bonus_cap"] = zahl("input_number.ne_zonnebonus_max_teruglevering", 6000)
    cfg["jahr_kwh"] = zahl("sensor.utilities_p1_meter_teruglevering_contractjaar", 0)
    cfg["belasting"] = zahl("input_number.ne_energiebelasting", 0)
    cfg["verkoop"] = zahl("input_number.ne_verkoopvergoeding", 0)
    inkoop = zahl("input_number.ne_inkoopvergoeding", -1)
    cfg["inkoop"] = inkoop if inkoop >= 0 else None
    try:
        cfg["saldering"] = (zustand("input_boolean.ne_saldering_actief")["state"] == "on")
    except Exception:
        # Helfer nicht lesbar: Rueckfall nach Kalender (Saldering endet am
        # 31.12.2026), und zwar laut, damit ein geloeschter Helfer auffaellt.
        cfg["saldering"] = jetzt().year < 2027
        log("WARNUNG: input_boolean.ne_saldering_actief nicht lesbar, nehme Saldering=%s (Kalender)."
            % cfg["saldering"])
    cfg["rt"] = zahl("input_number.gate_rt_wirkungsgrad", 84) / 100.0
    cfg["puffer"] = zahl("input_number.gate_puffer", 0.01)
    entl_ha = zahl("number.marstek_venus_modbus_maximale_entladeleistung", 800)
    cfg["entl_max_w"] = min(ENTL_MAX_W_HART, int(entl_ha)) if entl_ha > 0 else ENTL_MAX_W_HART
    cfg["v_faktor"] = 1.0
    if cfg["jahr_kwh"] > cfg["bonus_cap"] and cfg["jahr_kwh"] > 0:
        cfg["v_faktor"] = cfg["bonus_cap"] / cfg["jahr_kwh"]
    cfg["venster_von"], cfg["venster_bis"] = 6, 23
    try:
        vs = zustand("input_datetime.ne_zonnebonus_start")["state"]
        ve = zustand("input_datetime.ne_zonnebonus_ende")["state"]
        cfg["venster_von"] = int(vs[0:2])
        cfg["venster_bis"] = int(ve[0:2])
    except Exception:
        log("WARNUNG: Zonnebonus-Fenster nicht lesbar, nehme 06-23 Uhr.")
    return cfg


def beurs_karte():
    karte = {}
    kurve = zustand("sensor.stroomprijzen_48u")["attributes"].get("prices") or []
    for p in kurve:
        try:
            t = parse_iso(p[0])
            karte[t.strftime("%Y-%m-%d %H")] = float(p[1])
        except (ValueError, TypeError, IndexError):
            continue
    return karte


def ne_karte():
    # NextEnergy-Stundenpreise (all-in, EUR/kWh) des laufenden Tages aus dem
    # Attribut hourly_prices, Schluessel wie in beurs_karte(). Liegt fuer eine
    # Stunde ein Wert vor, ersetzt er "Beurs + Aufschlag der laufenden Stunde":
    # der eine Aufschlag rundete je Stunde um +/-1 ct daneben (Befund
    # 2026-09-03: 0,14 um 05:48, 0,13 um 06:48, die Morgenmarge kippte mit).
    karte = {}
    try:
        eintraege = zustand("sensor.next_energy_huidige_prijs")["attributes"].get("hourly_prices") or []
    except Exception:
        return karte
    for p in eintraege:
        try:
            t = parse_iso(p["start"])
            karte[t.strftime("%Y-%m-%d %H")] = float(p["eur_kwh"])
        except (ValueError, TypeError, KeyError):
            continue
    return karte


def einkaufs_offset(karte, cfg=None):
    # Aufschlag auf den Beurs-Preis (inkl. BTW) fuer Stunden ohne NextEnergy-
    # Stundenpreis (Folgetag): Energiebelasting + Inkoopvergoeding, beides
    # feste Vertragswerte aus den HA-Helfern (Andre 2026-09-03: "der
    # Aufschlag ist doch fest konfigurierbar"). Die fruehere Ableitung aus
    # huidige - Beurs der laufenden Stunde schwankte durch die Cent-Rundung
    # beider Sensoren um +/-1 ct (0,14 um 05:48, 0,13 um 06:48) und kippte
    # damit Morgenmargen mit. Die Live-Ableitung bleibt als Plausibilitaets-
    # pruefung: weicht sie deutlich ab, stimmen die Helfer nicht mehr.
    huidige = zahl("sensor.next_energy_huidige_prijs", float("nan"))
    schluessel = jetzt().strftime("%Y-%m-%d %H")
    live = None
    # NaN-Logik der PS-Fassung: ein legitim negativer Live-Preis darf nicht in
    # den Fallback kippen, nur ein fehlender Wert.
    if (not math.isnan(huidige)) and schluessel in karte:
        live = huidige - karte[schluessel]
    if cfg and cfg.get("inkoop") is not None:
        fest = cfg["belasting"] + cfg["inkoop"]
        if live is not None and abs(live - fest) > 0.015:
            log("WARNUNG: Live-Aufschlag %.3f EUR/kWh (huidige - Beurs) weicht vom konfigurierten %.4f "
                "(Belasting + Inkoopvergoeding) ab, Helfer pruefen." % (live, fest))
        return fest
    if live is not None:
        return live
    return 0.14


def wert_terug(beurs_wert, h, cfg):
    # Teruglever-Wert einer Stunde nach dem NextEnergy-Modell (live aus HA-Helfern).
    bonus = 0.0
    if cfg["venster_von"] <= h < cfg["venster_bis"] and beurs_wert > 0:
        bonus = cfg["bonus_pct"] * beurs_wert * cfg["v_faktor"]
    bel = cfg["belasting"] if cfg["saldering"] else 0.0
    return round(beurs_wert - cfg["verkoop"] + bonus + bel, 4)


def preis_kurven(tag0, karte, offset, cfg, karte_ne=None):
    # imp/ter je Stunde; fehlende Beurs-Stunden werden mit Sperr-Preisen
    # (999/-999) vom Handel ausgeschlossen statt als Phantom-0 gerechnet.
    # imp: NextEnergy-Stundenpreis wenn vorhanden (heute), sonst Beurs+Aufschlag.
    imp = [0.0] * 24
    ter = [0.0] * 24
    beurs = [0.0] * 24
    fehlt = [False] * 24
    tag_key = tag0.strftime("%Y-%m-%d")
    for h in range(24):
        key = "%s %02d" % (tag_key, h)
        if key not in karte:
            fehlt[h] = True
            imp[h] = 999.0
            ter[h] = -999.0
            continue
        beurs[h] = karte[key]
        imp[h] = preis_import(key, karte, offset, karte_ne, beurs[h] + offset)
        ter[h] = wert_terug(beurs[h], h, cfg)
    return {"imp": imp, "ter": ter, "beurs": beurs, "fehlt": fehlt}


W_LADEN_NETZ = "sensor.batterij_laden_uit_net_w"
W_LADEN_PV = "sensor.batterij_laden_uit_pv_w"


def integriere_fenster(punkte, von, bis):
    # Zeitgewichtete Integration einer W-Stufenkurve ueber ein beliebiges
    # Fenster, Ergebnis je Kalenderstunde ("YYYY-MM-DD HH" -> kWh). Gleiche
    # Stufenlogik wie integriere_stunden, nur mit freien Fenstergrenzen.
    kwh = {}
    if not punkte:
        return kwh
    start = von.replace(minute=0, second=0, microsecond=0)
    while start < bis:
        a = max(start, von)
        b = min(start + timedelta(hours=1), bis)
        wert = None
        for (t, v) in punkte:
            if t <= a:
                wert = v
            else:
                break
        t0 = a
        summe_wh = 0.0
        for (t, v) in punkte:
            if t <= a:
                continue
            if t >= b:
                break
            if wert is not None:
                summe_wh += wert * (t - t0).total_seconds() / 3600.0
            t0 = t
            wert = v
        if wert is not None:
            summe_wh += wert * (b - t0).total_seconds() / 3600.0
        if summe_wh > 0:
            kwh[start.strftime("%Y-%m-%d %H")] = summe_wh / 1000.0
        start += timedelta(hours=1)
    return kwh


def preis_import(key, karte, offset, karte_ne, fallback):
    # Einkaufspreis einer Kalenderstunde (EUR/kWh all-in): NextEnergy-
    # Stundenpreis, wenn vorhanden; sonst Beurs + Aufschlag; sonst Fallback.
    if karte_ne and key in karte_ne:
        return karte_ne[key]
    if key in karte:
        return karte[key] + offset
    return fallback


def einstand_pflegen(state, cfg, karte, offset, karte_ne=None):
    # Fuehrt den ECHTEN gewichteten Einkaufspreis der gespeicherten Energie im
    # State mit (statt der 15-ct-Konstante, Befund 2026-09-01: die 04:00-Ladung
    # zu 32 ct all-in wurde ab 04:55 als 15-ct-Ware verplant). Netzladung
    # kostet den Einkaufspreis ihrer Stunde, PV-Ladung ihre Export-
    # Opportunitaet (ter). Entladung aendert den Durchschnittspreis nicht.
    # Einheit: EUR je GESPEICHERTER kWh.
    #
    # 1.3.1: Zugang seit dem letzten Lauf wird aus den LIVE-Leistungssensoren
    # je Kalenderstunde integriert und je Stunde bepreist. Die frueheren
    # kWh-Integrationshelfer tickten bei konstanter Ladung stundenlang nicht
    # und buchten dann alles in einem Schub (Befund 2026-09-03: 1,59 kWh der
    # Stunden 4/5 kamen um 06:01, wurden mit dem Preis der Fenster-Mitte
    # Stunde 6 bewertet, und der Snapshot-SoC enthielt die Ware schon, also
    # doppelt gewichtet; vorher war der Einstand um 04:48 zu niedrig, weil
    # 1,4 kWh frische Stunde-3-Ware als Altbestand zum Vortagspreis galten).
    sqrt_rt = math.sqrt(cfg["rt"])
    boden = KAP_KWH * BODEN_PCT / 100.0
    soc_pct = zahl("sensor.marstek_venus_modbus_soc_batterie", -1)
    einstand = float(state.get("einstand", -1))
    if einstand < 0:
        einstand = float(state.get("einstand_start_fallback", 0.15))
    if soc_pct < 0:
        log("WARNUNG: Einstand-Update uebersprungen (SoC nicht lesbar), bleibe bei %.1f ct." % (einstand * 100))
        return einstand

    nun = jetzt()
    snap = state.get("einstand_snap")
    if snap:
        try:
            alt_zeit = datetime.fromisoformat(snap["zeit"])
            dt_h = (nun - alt_zeit).total_seconds() / 3600.0
            if dt_h > 3:
                log("WARNUNG: Einstand-Fenster %.1f h zu lang (seit %s), Zugang darin bleibt unbewertet."
                    % (dt_h, snap["zeit"]))
            elif dt_h > 0:
                netz_h = integriere_fenster(serie(W_LADEN_NETZ, alt_zeit, nun), alt_zeit, nun)
                pv_h = integriere_fenster(serie(W_LADEN_PV, alt_zeit, nun), alt_zeit, nun)
                dn = min(6.0, sum(netz_h.values()))
                dp = min(6.0, sum(pv_h.values()))
                if (dn + dp) > 0.02:
                    huidige = zahl("sensor.next_energy_huidige_prijs", float("nan"))
                    fallback = huidige if not math.isnan(huidige) else 0.30
                    kost = 0.0
                    for key, kwh in netz_h.items():
                        kost += kwh * preis_import(key, karte, offset, karte_ne, fallback)
                    for key, kwh in pv_h.items():
                        beurs_h = karte.get(key)
                        if beurs_h is None:
                            beurs_h = max(0.0, fallback - offset)
                        kost += kwh * wert_terug(beurs_h, int(key[-2:]), cfg)
                    e_alt = max(0.0, KAP_KWH * float(snap["soc"]) / 100.0 - boden)
                    zugang = (dn + dp) * sqrt_rt
                    if e_alt < 0.05:
                        einstand = kost / zugang  # Akku war leer: nur die neue Ware zaehlt
                    else:
                        einstand = (e_alt * einstand + kost) / (e_alt + zugang)
                    einstand = min(1.5, max(0.0, einstand))
                    stunden = ", ".join(k[-2:] + "h" for k in sorted(set(list(netz_h) + list(pv_h))))
                    log("Einstand aktualisiert: %.1f ct/kWh (Zugang %.2f kWh Netz / %.2f kWh PV, Stunden %s)"
                        % (einstand * 100, dn, dp, stunden))
        except (ValueError, KeyError, TypeError) as e:
            log("WARNUNG: Einstand-Snapshot unlesbar (%s), setze neu auf." % e)
        except Exception as e:
            # HA-API nicht erreichbar: Fenster offen lassen, der naechste Lauf
            # holt den Zugang nach (bis zur 3-h-Grenze).
            log("WARNUNG: Einstand-Update uebersprungen (%s), Fenster bleibt offen." % e)
            return einstand
    state["einstand_snap"] = {"zeit": nun.isoformat(), "soc": soc_pct}
    state["einstand"] = round(einstand, 4)
    try:
        schreibe_json(STATE_PATH, state)
    except OSError as e:
        log("WARNUNG: State nicht schreibbar (%s)." % e)
    return einstand


def ist_dst_tag(tag0):
    return tag0.utcoffset() != (tag0 + timedelta(days=1)).utcoffset()


def solcast_stunden(entity, tag0, feld):
    pv = [0.0] * 24
    try:
        eintraege = zustand(entity)["attributes"].get("detailedHourly") or []
    except Exception:
        log("WARNUNG: Solcast %s nicht lesbar, PV = 0." % entity)
        return pv
    for e in eintraege:
        try:
            t = parse_iso(e["period_start"])
        except (ValueError, KeyError):
            continue
        if t.date() == tag0.date():
            pv[t.hour] = float(e.get(feld) or 0.0)
    return pv


# ------------------------------------------------------- Haus-Lastprofil (State)
def tages_profil(tag0):
    # Haus-Last je Stunde eines VOLLEN vergangenen Tages aus den Zaehlerkanten
    # (Netto ohne Batterie = Import - Export + Entladung - Ladung, plus PV-Ist).
    if ist_dst_tag(tag0):
        return None
    kanten = {}
    for name, entity in ZAEHLER.items():
        # Enges Fenster zuerst (P1-Zaehler sind gespraechig); nur wenn vor der
        # ersten Kante kein Punkt liegt (stiller Batterie-Zaehler), einmal mit
        # 48 h Rueckschau nachfassen (Muster aus Get-HaKantenWert der PS-Fassung).
        punkte = serie(entity, tag0 - timedelta(hours=2), tag0 + timedelta(hours=24, minutes=5))
        if not punkte or punkte[0][0] > tag0:
            punkte = serie(entity, tag0 - timedelta(hours=48), tag0 + timedelta(hours=24, minutes=5))
        werte = [None] * 25
        for h in range(25):
            kante = tag0 + timedelta(hours=h)
            w = None
            for (t, v) in punkte:
                if t <= kante:
                    w = v
                else:
                    break
            werte[h] = w
        # Fuehrende Luecken RUECKWAERTS fuellen, sonst wird das erste Delta zum
        # kompletten Lebenszeit-Zaehlerstand. Danach Vorwaerts-Fuellung.
        erster = next((i for i, w in enumerate(werte) if w is not None), -1)
        if erster < 0:
            return None
        for i in range(erster):
            werte[i] = werte[erster]
        for i in range(1, 25):
            if werte[i] is None:
                werte[i] = werte[i - 1]
        kanten[name] = werte

    def deltas(w):
        d = [0.0] * 24
        for h in range(24):
            x = w[h + 1] - w[h]
            if x < -0.001 or x > 25:
                x = 0.0  # Zaehlerruecksprung oder unplausibles Delta
            d[h] = max(0.0, x)
        return d

    imp = deltas(kanten["imp"])
    exp = deltas(kanten["exp"])
    dis = deltas(kanten["dis"])
    lad = deltas(kanten["lad"])
    pv_punkte = serie("sensor.solcast_pv_forecast_aktuelle_leistung",
                      tag0, tag0 + timedelta(hours=24))
    pv_ist = integriere_stunden(pv_punkte, tag0)
    haus = [0.0] * 24
    for h in range(24):
        netto = imp[h] - exp[h] + dis[h] - lad[h]
        haus[h] = round(netto + pv_ist[h], 4)
    return haus


def profil_auffuellen(state, profil_tage):
    # Fehlende volle Tage (gestern rueckwaerts) aus der HA-Historie nachrechnen.
    # Recorder haelt ~10 Tage; nicht rechenbare Tage werden als leer markiert.
    heute0 = jetzt().replace(hour=0, minute=0, second=0, microsecond=0)
    neu = 0
    if state.get("profil_version") != PROFIL_VERSION:
        if state.get("tage"):
            log("Profil-Cache verworfen (Zaehlerquelle geaendert, Version %d), Tage werden neu gerechnet."
                % PROFIL_VERSION)
        state["tage"] = {}
        state["profil_version"] = PROFIL_VERSION
        neu += 1  # Version auch dann persistieren, wenn kein Tag rechenbar ist
    for delta in range(1, min(profil_tage, 10) + 1):
        tag0 = heute0 - timedelta(days=delta)
        key = tag0.strftime("%Y-%m-%d")
        if key in state["tage"]:
            continue
        try:
            haus = tages_profil(tag0)
        except Exception as e:
            log("WARNUNG: Profil %s nicht rechenbar (%s), spaeter erneut." % (key, e))
            continue
        if haus is None:
            state["tage"][key] = {"leer": True}
            log("Profil %s: keine Zaehlerdaten (oder DST), als leer markiert." % key)
        else:
            state["tage"][key] = {"haus": haus, "stunden": 24}
            log("Profil %s aus HA-Historie aufgebaut." % key)
            neu += 1
    # beschneiden: nur die juengsten profil_tage + 6 Eintraege behalten
    keys = sorted(state["tage"].keys(), reverse=True)[: profil_tage + 6]
    state["tage"] = {k: state["tage"][k] for k in keys}
    if neu:
        schreibe_json(STATE_PATH, state)
    return state


def haus_profil(state, profil_tage=None):
    # Median je Stunde ueber die juengsten `profil_tage` Tage MIT Daten;
    # Notnagel 350 W Grundlast. Befund 2026-09-03: profil_auffuellen behaelt
    # profil_tage + 6 Tage als Vorrat, der Median lief aber ueber ALLE
    # behaltenen Tage (Einstellung 3 wirkte als 9-Tage-Median).
    profil = [0.35] * 24
    tage = []
    for key in sorted(state["tage"].keys(), reverse=True):
        eintrag = state["tage"][key]
        if eintrag.get("haus") is None:
            continue
        tage.append(eintrag)
        if profil_tage and len(tage) >= int(profil_tage):
            break
    for h in range(24):
        werte = []
        for eintrag in tage:
            haus = eintrag["haus"]
            anz = int(eintrag.get("stunden", 24))
            if h < anz:
                werte.append(float(haus[h]))
        if werte:
            werte.sort()
            profil[h] = werte[(len(werte) - 1) // 2]
    return profil


# ------------------------------------------------------------------ Optimierer
def reservationswert(pk, cfg, export_ok):
    # Wert einer GESPEICHERTEN kWh ueber den Planhorizont hinaus (1.3.5), EUR/kWh.
    # Der Einstand ist bezahlt (Sunk Cost) und taugt nicht als Verkaufsgrenze:
    # unter Saldering lieferte der um 11 ct hoehere Exportwert fast immer eine
    # Abend-Ankerbuchung, die der Umschicht-Pass nach vorn zog; ohne Saldering
    # (Replay 2026-09-04 des 06:48-Laufs vom 03.09., Einstand 33,4 ct) blieben
    # 2,44 kWh den ganzen Tag liegen, waehrend das Haus morgens zu 36 ct aus dem
    # Netz lief. Massstab ist darum, was die kWh spaeter noch bringt, mit zwei
    # Deckeln: (a) die beste Stunde des Referenztags (Haus zum Einkaufspreis, im
    # Bonusfenster Export zum Teruglever-Wert), (b) der Nachkauf in der billigsten
    # Stunde des Referenztags, denn mehr als den Nachkauf ist Halten nie wert.
    # Referenztag ist der Folgetag, sobald seine Kurve vorliegt, sonst der
    # geplante Tag selbst (siehe rechne_und_publiziere).
    sqrt_rt = math.sqrt(cfg["rt"])
    best = None
    billig = None
    for h in range(24):
        if pk["fehlt"][h] or pk["imp"][h] > 500:
            continue
        w = pk["imp"][h]
        if export_ok is None or export_ok[h]:
            w = max(w, pk["ter"][h])
        best = w if best is None else max(best, w)
        billig = pk["imp"][h] if billig is None else min(billig, pk["imp"][h])
    if best is None:
        return None
    return round(max(0.0, min(sqrt_rt * best, billig / sqrt_rt)), 4)


def optimiere(imp, ter, netto, soc_start_kwh, einstand_start, rt, puffer, entl_max_w,
              export_ok=None, reservation=None):
    # export_ok: je Stunde True/False, ob Entladung ueber den Hausbedarf hinaus
    # (Export aus dem Akku) erlaubt ist; None = ueberall erlaubt. Andre
    # 2026-09-03: Export nur im Zonnebonus-Fenster, sonst Nulleinspeisung.
    # reservation: Halte-Schwelle fuer Startinhalt in EUR je gespeicherter kWh
    # (siehe reservationswert); None = Einstand als Schwelle (Regel bis 1.3.4,
    # nur noch als Referenz im Selbsttest).
    # Voll-dynamischer, verlustfreier Stunden-Optimierer (greedy best-pair).
    # 1:1-Port aus batterie_schatten.ps1; Eingaben in Meter-kWh, SoC-Bahn in
    # gespeicherten kWh.
    n = len(netto)
    sqrt_rt = math.sqrt(rt)
    boden = KAP_KWH * BODEN_PCT / 100.0
    entl_max = entl_max_w / 1000.0
    lad_netz = LADEN_NETZ_MAX_W / 1000.0
    lad_pv = LADEN_PV_MAX_W / 1000.0

    halte = einstand_start if reservation is None else reservation

    bedarf = [0.0] * n
    pv_frei = [0.0] * n
    for h in range(n):
        if netto[h] > 0:
            bedarf[h] = netto[h]
        else:
            pv_frei[h] = -netto[h]
    lad_netz_m = [0.0] * n
    lad_pv_m = [0.0] * n
    entl_haus = [0.0] * n
    entl_exp = [0.0] * n
    soc = [soc_start_kwh] * n
    start_frei = max(0.0, soc_start_kwh - boden)
    trades = []
    struktur = []  # maschinenlesbare Trade-Liste fuer den Umschicht-Pass

    def richtung_ok(h, soll):
        laedt = (lad_netz_m[h] + lad_pv_m[h]) > 1e-9
        entlaedt = (entl_haus[h] + entl_exp[h]) > 1e-9
        return (not entlaedt) if soll == "laden" else (not laedt)

    def block_kandidaten(k, raus_max):
        # Block-Bewertung (1.2.0): Entladung in Stunde k wird Haus-zuerst
        # abgerechnet (nettierender P1: solange die Stunde Netzbezug hat,
        # mindert Entladung nur den Import, erst danach ist es Export).
        # Kandidaten in GELIEFERTEN kWh: (a) nur den Hausbedarf decken,
        # (b) voller Block = Haus decken + Rest exportieren, bewertet zum
        # gemischten kWh-Wert. So konkurriert eine Stunde mit kleinem
        # Rest-Hausbedarf als ganzer Block statt als Kruemel (Befund
        # 2026-09-01: 800-W-Block ging an die reine Exportstunde 10 statt
        # an die bessere Mischstunde 9).
        kand = []
        if raus_max <= 1e-6:
            return kand
        h_teil = min(bedarf[k], raus_max)
        if h_teil > 1e-6:
            kand.append((h_teil, imp[k]))
        if export_ok is not None and not export_ok[k]:
            return kand  # ausserhalb des Bonusfensters: nur Hausbedarf, kein Exportanteil
        if raus_max - h_teil > 1e-6:
            wert = (h_teil * imp[k] + (raus_max - h_teil) * ter[k]) / raus_max
            kand.append((raus_max, wert))
        return kand

    for _ in range(MAX_ITER):
        best_marge = puffer - 1e-12
        best = None
        for k in range(n):
            if not richtung_ok(k, "entladen"):
                continue
            entl_rest = entl_max - (entl_haus[k] + entl_exp[k])
            if entl_rest <= 1e-9:
                continue
            # --- Quelle Startinhalt (jederzeit verfuegbar) ---
            if start_frei > 1e-9:
                frei = start_frei
                for t in range(k, n):
                    frei = min(frei, soc[t] - boden)
                raus_max = min(frei * sqrt_rt, entl_rest)
                for raus, wert in block_kandidaten(k, raus_max):
                    m = sqrt_rt * wert - halte
                    if m > best_marge:
                        h_raus = min(bedarf[k], raus)
                        best_marge = m
                        best = {"art": "start", "s": -1, "k": k, "meter": raus,
                                "h_raus": h_raus, "e_raus": raus - h_raus}
            # --- Quellen Netz / PV in frueherer Stunde ---
            for s in range(k):
                if not richtung_ok(s, "laden"):
                    continue
                for quelle in ("netz", "pv"):
                    if quelle == "pv" and pv_frei[s] <= 1e-9:
                        continue
                    kost = imp[s] if quelle == "netz" else ter[s]
                    # Gesamt-Ladekappe: Netz+PV zusammen nie ueber Hardware-Max.
                    gesamt_rest = lad_pv - (lad_netz_m[s] + lad_pv_m[s])
                    if quelle == "netz":
                        lad_rest = min(lad_netz - lad_netz_m[s], gesamt_rest)
                    else:
                        lad_rest = min(lad_pv - lad_pv_m[s], pv_frei[s], gesamt_rest)
                    if lad_rest <= 1e-9:
                        continue
                    kopf = KAP_KWH - soc[s]
                    for t in range(s, k):
                        kopf = min(kopf, KAP_KWH - soc[t])
                    raus_max = min(lad_rest, kopf / sqrt_rt, entl_rest / rt) * rt
                    for raus, wert in block_kandidaten(k, raus_max):
                        m = rt * wert - kost
                        if m > best_marge:
                            h_raus = min(bedarf[k], raus)
                            best_marge = m
                            best = {"art": quelle, "s": s, "k": k, "meter": raus / rt,
                                    "h_raus": h_raus, "e_raus": raus - h_raus}
        if best is None:
            break
        k = best["k"]
        if best["art"] == "start":
            st = best["meter"] / sqrt_rt
            start_frei -= st
            for t in range(k, n):
                soc[t] -= st
        else:
            s = best["s"]
            st = best["meter"] * sqrt_rt
            if best["art"] == "netz":
                lad_netz_m[s] += best["meter"]
            else:
                lad_pv_m[s] += best["meter"]
                pv_frei[s] -= best["meter"]
            for t in range(s, k):
                soc[t] += st
        # Block ggf. in Haus- und Export-Anteil aufteilen (getrennte Buchungen,
        # damit Simulator und Bilanz je Senke korrekt rechnen).
        for senke, raus in (("haus", best["h_raus"]), ("export", best["e_raus"])):
            if raus <= 1e-9:
                continue
            if best["art"] == "start":
                marge = sqrt_rt * (imp[k] if senke == "haus" else ter[k]) - einstand_start
                trades.append("Startinhalt %.2f kWh -> %s %02dh (%.1f ct Marge/kWh)"
                              % (raus, senke, k, marge * 100))
                struktur.append({"art": "start", "s": -1, "k": k, "senke": senke,
                                 "meter": raus})
            else:
                s = best["s"]
                kost = imp[s] if best["art"] == "netz" else ter[s]
                marge = rt * (imp[k] if senke == "haus" else ter[k]) - kost
                trades.append("%s %02dh %.2f kWh -> %s %02dh (%.1f ct Marge/kWh)"
                              % (best["art"], s, raus / rt, senke, k, marge * 100))
                struktur.append({"art": best["art"], "s": s, "k": k, "senke": senke,
                                 "meter": raus / rt})
            if senke == "haus":
                entl_haus[k] += raus
                bedarf[k] = max(0.0, bedarf[k] - raus)
            else:
                entl_exp[k] += raus

    eur = 0.0
    for h in range(n):
        eur += (pv_frei[h] + entl_exp[h]) * ter[h] - (bedarf[h] + lad_netz_m[h]) * imp[h]
    return {"eur": round(eur, 4), "lad_netz": lad_netz_m, "lad_pv": lad_pv_m,
            "entl_haus": entl_haus, "entl_exp": entl_exp, "soc": soc,
            "trades": trades, "struktur": struktur}


def simuliere(struktur, imp, ter, netto, soc_start_kwh, einstand, rt, entl_max_w):
    # Spielt eine Trade-Liste von Null durch und prueft ALLE Grenzen unabhaengig
    # vom Greedy. Rueckgabe hat dieselbe Form wie optimiere() plus "ok".
    n = len(netto)
    sqrt_rt = math.sqrt(rt)
    boden = KAP_KWH * BODEN_PCT / 100.0
    entl_max = entl_max_w / 1000.0
    lad_netz = LADEN_NETZ_MAX_W / 1000.0
    lad_pv = LADEN_PV_MAX_W / 1000.0
    bedarf0 = [max(0.0, x) for x in netto]
    pv_frei0 = [max(0.0, -x) for x in netto]

    lad_netz_m = [0.0] * n
    lad_pv_m = [0.0] * n
    entl_haus = [0.0] * n
    entl_exp = [0.0] * n
    soc = [soc_start_kwh] * n
    start_verbraucht = 0.0
    trades = []
    for t in struktur:
        m = t["meter"]
        if m <= 1e-9:
            continue
        k = t["k"]
        if t["art"] == "start":
            st = m / sqrt_rt
            start_verbraucht += st
            for x in range(k, n):
                soc[x] -= st
            raus = m
            marge = sqrt_rt * (imp[k] if t["senke"] == "haus" else ter[k]) - einstand
            trades.append("Startinhalt %.2f kWh -> %s %02dh (%.1f ct Marge/kWh)"
                          % (raus, t["senke"], k, marge * 100))
        else:
            s = t["s"]
            st = m * sqrt_rt
            if t["art"] == "netz":
                lad_netz_m[s] += m
            else:
                lad_pv_m[s] += m
            for x in range(s, k):
                soc[x] += st
            raus = m * rt
            kost = imp[s] if t["art"] == "netz" else ter[s]
            marge = rt * (imp[k] if t["senke"] == "haus" else ter[k]) - kost
            trades.append("%s %02dh %.2f kWh -> %s %02dh (%.1f ct Marge/kWh)"
                          % (t["art"], s, m, t["senke"], k, marge * 100))
        if t["senke"] == "haus":
            entl_haus[k] += raus
        else:
            entl_exp[k] += raus

    eps = 1e-6
    ok = True
    if start_verbraucht > max(0.0, soc_start_kwh - boden) + eps:
        ok = False
    boden_min = min(boden, soc_start_kwh)
    for h in range(n):
        laedt = lad_netz_m[h] + lad_pv_m[h]
        entl = entl_haus[h] + entl_exp[h]
        if entl > entl_max + eps or lad_netz_m[h] > lad_netz + eps or laedt > lad_pv + eps:
            ok = False
        if laedt > eps and entl > eps:
            ok = False
        if lad_pv_m[h] > pv_frei0[h] + eps:
            ok = False
        if entl_haus[h] > bedarf0[h] + eps:
            ok = False
        # Export erst, wenn der Hausbedarf der Stunde gedeckt ist (nettierender P1)
        if entl_exp[h] > eps and (bedarf0[h] - entl_haus[h]) > 1e-3:
            ok = False
        if soc[h] < boden_min - eps or soc[h] > KAP_KWH + eps:
            ok = False

    eur = 0.0
    for h in range(n):
        bedarf_rest = max(0.0, bedarf0[h] - entl_haus[h])
        pv_rest = max(0.0, pv_frei0[h] - lad_pv_m[h])
        eur += (pv_rest + entl_exp[h]) * ter[h] - (bedarf_rest + lad_netz_m[h]) * imp[h]
    return {"ok": ok, "eur": round(eur, 4), "lad_netz": lad_netz_m, "lad_pv": lad_pv_m,
            "entl_haus": entl_haus, "entl_exp": entl_exp, "soc": soc,
            "trades": trades, "struktur": struktur}


def verbessere(opt, imp, ter, netto, soc_start_kwh, einstand, rt, puffer, entl_max_w,
               export_ok=None):
    # Umschicht-Pass gegen die Greedy-Luecke (Anlass 2026-09-01): Energie, die im
    # Akku ueber eine teure fruehe Stunde k1 hinweg fuer eine spaetere Stunde k2
    # gebucht ist, wird bei k1 verkauft und fuer k2 in einer billigen Stunde s
    # (k1 < s < k2) nachgekauft. Lohnt sich, sobald wert(k1) > kost(s)/rt.
    # SICHERUNG: jede Umschichtung muss den unabhaengigen Simulator bestehen und
    # die Bilanz strikt verbessern, sonst bleibt das Greedy-Ergebnis stehen.
    n = len(netto)
    sqrt_rt = math.sqrt(rt)
    entl_max = entl_max_w / 1000.0
    lad_netz = LADEN_NETZ_MAX_W / 1000.0
    lad_pv = LADEN_PV_MAX_W / 1000.0
    bedarf0 = [max(0.0, x) for x in netto]

    basis = simuliere(opt["struktur"], imp, ter, netto, soc_start_kwh, einstand, rt, entl_max_w)
    if not basis["ok"] or abs(basis["eur"] - opt["eur"]) > 1e-3:
        log("WARNUNG: Simulator weicht vom Greedy ab (%.4f vs %.4f), Umschicht-Pass uebersprungen."
            % (basis["eur"], opt["eur"]))
        return opt
    best = basis
    umschichtungen = 0
    verworfen = set()  # an der Simulation gescheiterte Kandidaten (pro Struktur-Stand)

    for _ in range(48):
        kandidat = None
        st = best["struktur"]
        for idx, t in enumerate(st):
            k2 = t["k"]
            quelle_ab = 0 if t["art"] == "start" else t["s"]
            raus_alt = t["meter"] if t["art"] == "start" else t["meter"] * rt
            if raus_alt < 0.05:
                continue
            for k1 in range(quelle_ab, k2):
                if imp[k1] > 500:
                    continue
                entl_rest_k1 = entl_max - (best["entl_haus"][k1] + best["entl_exp"][k1])
                if entl_rest_k1 < 0.05:
                    continue
                bedarf_rest_k1 = max(0.0, bedarf0[k1] - best["entl_haus"][k1])
                for s in range(k1 + 1, k2):
                    if imp[s] > 500:
                        continue
                    for nach_art in ("netz", "pv"):
                        if nach_art == "pv":
                            pv_rest_s = max(0.0, -netto[s]) - best["lad_pv"][s]
                            if pv_rest_s < 0.05:
                                continue
                            kost = ter[s]
                            lad_rest = min(lad_pv - (best["lad_netz"][s] + best["lad_pv"][s]), pv_rest_s)
                        else:
                            kost = imp[s]
                            lad_rest = min(lad_netz - best["lad_netz"][s],
                                           lad_pv - (best["lad_netz"][s] + best["lad_pv"][s]))
                        if lad_rest < 0.05:
                            continue
                        wert2 = imp[k2] if t["senke"] == "haus" else ter[k2]
                        if rt * wert2 - kost <= puffer:
                            continue
                        raus_cap = min(raus_alt, entl_rest_k1, lad_rest * rt)
                        if raus_cap < 0.05:
                            continue
                        h_teil_max = min(bedarf_rest_k1, raus_cap)
                        # Block-Bewertung (1.2.0) wie im Greedy: Variante "haus"
                        # deckt nur den Hausbedarf, Variante "block" verkauft den
                        # vollen Block (Haus zuerst, Rest Export) zum gemischten
                        # kWh-Wert. Ersetzt die alte haus/export-Trennung, bei
                        # der eine Stunde mit kleinem Rest-Hausbedarf nie einen
                        # vollen Block bekommen konnte (Befund 2026-09-01).
                        for variante in ("haus", "block"):
                            if variante == "haus":
                                raus = h_teil_max
                                if raus < 0.05:
                                    continue
                                h_teil, e_teil = raus, 0.0
                                wert1 = imp[k1]
                            else:
                                raus = raus_cap
                                h_teil = h_teil_max
                                e_teil = raus - h_teil
                                if e_teil < 1e-6:
                                    continue  # identisch mit Variante "haus"
                                if export_ok is not None and not export_ok[k1]:
                                    continue  # kein Export ausserhalb des Bonusfensters
                                wert1 = (h_teil * imp[k1] + e_teil * ter[k1]) / raus
                            # Gate fuer das neue fruehe Entladen: nur fuer noch
                            # NICHT gekaufte Ware (netz/pv-Quelle) muss der
                            # Verkauf ueber dem Einkauf liegen. Startinhalt ist
                            # bezahlt (Sunk Cost); sein Massstab ist allein der
                            # Nachkauf in s, den die Kern-Ungleichung unten
                            # prueft. Befund 2026-09-03: das alte Einstand-Gate
                            # (sqrt_rt*wert1 - einstand > puffer) blockte bei
                            # 33,4 ct Einstand das Dreieck "07h zu 36 ct
                            # verkaufen, 14h zu 17 ct nachkaufen" (+16 ct/kWh);
                            # Stunden 7 und 8 blieben RUHE, ~1 kWh lag den
                            # Tag ueber ungenutzt im Akku.
                            if t["art"] != "start":
                                m1 = rt * wert1 - (imp[t["s"]] if t["art"] == "netz" else ter[t["s"]])
                                if m1 <= puffer:
                                    continue
                            # Kern-Ungleichung des Dreiecks
                            gewinn_kwh = wert1 - kost / rt
                            if gewinn_kwh <= puffer:
                                continue
                            key = (idx, k1, variante, s, nach_art)
                            if key in verworfen:
                                continue
                            if kandidat is None or gewinn_kwh * raus > kandidat["gewinn"]:
                                kandidat = {"idx": idx, "k1": k1, "s": s,
                                            "nach_art": nach_art, "key": key,
                                            "raus": raus, "h_teil": h_teil,
                                            "e_teil": e_teil,
                                            "gewinn": gewinn_kwh * raus}
        if kandidat is None:
            break
        t = st[kandidat["idx"]]
        raus = kandidat["raus"]
        neu = [dict(x) for x in st]
        if t["art"] == "start":
            neu[kandidat["idx"]]["meter"] = t["meter"] - raus
        else:
            neu[kandidat["idx"]]["meter"] = t["meter"] - raus / rt
        for senke1, teil in (("haus", kandidat["h_teil"]), ("export", kandidat["e_teil"])):
            if teil <= 1e-9:
                continue
            if t["art"] == "start":
                neu.append({"art": "start", "s": -1, "k": kandidat["k1"],
                            "senke": senke1, "meter": teil})
            else:
                neu.append({"art": t["art"], "s": t["s"], "k": kandidat["k1"],
                            "senke": senke1, "meter": teil / rt})
        neu.append({"art": kandidat["nach_art"], "s": kandidat["s"], "k": t["k"],
                    "senke": t["senke"], "meter": raus / rt})
        probe = simuliere(neu, imp, ter, netto, soc_start_kwh, einstand, rt, entl_max_w)
        if not probe["ok"] or probe["eur"] <= best["eur"] + 0.005:
            verworfen.add(kandidat["key"])
            continue
        if kandidat["e_teil"] <= 1e-9:
            beschr = "haus"
        elif kandidat["h_teil"] <= 1e-9:
            beschr = "export"
        else:
            beschr = "haus+export"
        log("UMSCHICHTUNG: %02dh-Buchung -> Verkauf %02dh (%s) + Nachkauf %02dh (%s), %.2f kWh, +%.2f EUR"
            % (t["k"], kandidat["k1"], beschr, kandidat["s"],
               kandidat["nach_art"], raus, probe["eur"] - best["eur"]))
        best = probe
        umschichtungen += 1
        verworfen = set()

    if umschichtungen == 0:
        return opt
    best["eur"] = round(best["eur"], 4)
    return best


def _selbsttest_kern():
    # Regressions-Selbsttest beim Start (rein lokal, ohne HA/Netz, < 1 s):
    # das 08:55-Szenario vom 2026-09-01, bei dem die alte haus/export-Trennung
    # die beste Stunde 9 (kleiner Rest-Hausbedarf 0.14 kWh) leer ausgehen
    # liess. Die Block-Bewertung muss ihr den vollen 800-W-Block geben.
    # Schlaegt ein Check fehl, beendet sich das Add-on OHNE Publish: der alte
    # retained Plan bleibt stehen, der anti_feed-Fallback im Haus greift.
    rt, puffer, einstand = 0.85, 0.01, 0.15
    beurs = [0.21, 0.19, 0.19, 0.18, 0.18, 0.19, 0.23, 0.25, 0.23, 0.19, 0.16,
             0.11, 0.06, 0.04, 0.05, 0.06, 0.12, 0.17, 0.22, 0.25, 0.28, 0.27,
             0.24, 0.22]
    cfg = {"bonus_pct": 0.5, "v_faktor": 1.0, "venster_von": 6,
           "venster_bis": 22, "belasting": 0.1108, "verkoop": 0.0,
           "saldering": True}
    imp = [b + 0.131 for b in beurs]
    ter = [wert_terug(beurs[h], h, cfg) for h in range(24)]
    netto = [0.0] * 24
    netto[9] = 0.14
    netto[12] = -0.2
    netto[13] = -0.5
    netto[14] = -0.4
    netto[15] = -0.3
    netto[19] = 0.23
    netto[20] = 0.75
    netto[21] = 0.25
    netto[22] = 0.94
    netto[23] = 0.30
    for h in range(9):  # Replan ab 09:00, fruehere Stunden gesperrt
        imp[h] = 999.0
        ter[h] = -999.0
    soc_start = 0.387 * KAP_KWH
    opt = optimiere(imp, ter, netto, soc_start, einstand, rt, puffer, 800)
    fein = verbessere(opt, imp, ter, netto, soc_start, einstand, rt, puffer, 800)
    sim = simuliere(fein["struktur"], imp, ter, netto, soc_start, einstand, rt, 800)
    fehler = []
    if not sim["ok"]:
        fehler.append("Simulator lehnt den Plan ab.")
    if abs(sim["eur"] - fein["eur"]) > 0.005:
        fehler.append("Simulator-Bilanz weicht ab (%.4f vs %.4f)."
                      % (sim["eur"], fein["eur"]))
    if fein["eur"] < opt["eur"] - 1e-6:
        fehler.append("Umschicht-Pass verschlechtert die Bilanz.")
    entl9 = sim["entl_haus"][9] + sim["entl_exp"][9]
    if entl9 < 0.7:
        fehler.append("Stunde 9 bekommt keinen vollen Block (%.2f kWh)." % entl9)
    if sim["entl_exp"][9] <= 1e-6:
        fehler.append("Stunde 9 exportiert nicht (Block-Bewertung wirkungslos).")
    for h in range(24):
        if sim["entl_haus"][h] + sim["entl_exp"][h] > 0.8 + 1e-6:
            fehler.append("Stunde %d ueber 800 W." % h)

    # Szenario 2 (1.3.0): der 06:48-Lauf vom 2026-09-03. Einstand 33,4 ct
    # liegt ueber dem Morgenwert (36 ct * sqrt(rt) = 33,2 ct), der Greedy
    # bucht 07h darum nicht; der Umschicht-Pass muss das Dreieck trotzdem
    # fahren (07h verkaufen, 14h zu 17 ct nachkaufen), weil der Einstand
    # bezahlt ist. Gegenprobe 2b: ist KEINE spaetere Stunde billiger als der
    # Morgenwert (imp/rt ueberall ueber 36 ct), darf nicht verkauft werden
    # (Kern-Ungleichung), sonst geht die Abendreserve verloren.
    einstand2 = 0.334
    beurs2 = [0.22, 0.21, 0.19, 0.17, 0.17, 0.17, 0.21, 0.23, 0.22, 0.18, 0.15,
              0.11, 0.06, 0.04, 0.04, 0.04, 0.08, 0.15, 0.22, 0.27, 0.27, 0.24,
              0.22, 0.21]
    netto2 = [0.0] * 24
    for h, w in ((7, 0.74), (8, 0.65), (9, 0.30), (10, 0.10), (11, -0.30),
                 (12, -0.80), (13, -1.0), (14, -1.2), (15, -1.4), (16, -1.3),
                 (17, -0.9), (18, 0.0), (19, 0.49), (20, 0.65), (21, 0.14),
                 (22, 0.94), (23, 0.30)):
        netto2[h] = w
    soc2 = 0.60 * KAP_KWH
    for variante, mittag in (("2", None), ("2b", 0.19)):
        b = list(beurs2)
        if mittag is not None:
            for h in range(8, 24):
                b[h] = max(b[h], mittag)  # imp >= 0.32, imp/rt >= 0.376 > 0.36
        imp2 = [x + 0.13 for x in b]
        ter2 = [wert_terug(b[h], h, cfg) for h in range(24)]
        for h in range(7):
            imp2[h] = 999.0
            ter2[h] = -999.0
        opt2 = optimiere(imp2, ter2, netto2, soc2, einstand2, rt, puffer, 800)
        fein2 = verbessere(opt2, imp2, ter2, netto2, soc2, einstand2, rt, puffer, 800)
        sim2 = simuliere(fein2["struktur"], imp2, ter2, netto2, soc2, einstand2, rt, 800)
        entl7 = sim2["entl_haus"][7] + sim2["entl_exp"][7]
        if not sim2["ok"]:
            fehler.append("Szenario %s: Simulator lehnt den Plan ab." % variante)
        if abs(sim2["eur"] - fein2["eur"]) > 0.005:
            fehler.append("Szenario %s: Simulator-Bilanz weicht ab (%.4f vs %.4f)."
                          % (variante, sim2["eur"], fein2["eur"]))
        if fein2["eur"] < opt2["eur"] - 1e-6:
            fehler.append("Szenario %s: Umschicht-Pass verschlechtert die Bilanz." % variante)
        for h in range(24):
            if sim2["entl_haus"][h] + sim2["entl_exp"][h] > 0.8 + 1e-6:
                fehler.append("Szenario %s: Stunde %d ueber 800 W." % (variante, h))
        if mittag is None:
            if entl7 < 0.5:
                fehler.append("Szenario 2: Stunde 7 bleibt trotz billigem Mittag leer "
                              "(%.2f kWh), Sunk-Cost-Gate wirkt noch." % entl7)
            if fein2["eur"] < opt2["eur"] + 0.05:
                fehler.append("Szenario 2: Umschicht-Pass bringt keinen Gewinn (%.4f vs %.4f)."
                              % (fein2["eur"], opt2["eur"]))
        elif entl7 > 1e-6:
            fehler.append("Szenario 2b: Stunde 7 verkauft ohne billigen Nachkauf "
                          "(%.2f kWh), Abendreserve geht verloren." % entl7)

    # Szenario 3 (1.3.4): Export nur im Bonusfenster 06-22. Abend des
    # 2026-09-03 (Einstand 26,3 ct, Akku 62 % ab 21 Uhr): mit Fenster darf in
    # Stunde 22/23 nichts exportiert werden, der Hausbedarf wird weiter
    # gedeckt (ohne Fenster plante 1.3.3 einen 800-W-Block mit Exportanteil).
    einstand3 = 0.263
    imp3 = [x + 0.1327 for x in beurs2]
    ter3 = [wert_terug(beurs2[h], h, cfg) for h in range(24)]
    netto3 = [0.0] * 24
    netto3[21], netto3[22], netto3[23] = 0.82, 0.72, 0.67
    for h in range(21):
        imp3[h] = 999.0
        ter3[h] = -999.0
    soc3 = 0.62 * KAP_KWH
    fenster = [6 <= h < 22 for h in range(24)]
    opt3 = optimiere(imp3, ter3, netto3, soc3, einstand3, rt, puffer, 800, fenster)
    fein3 = verbessere(opt3, imp3, ter3, netto3, soc3, einstand3, rt, puffer, 800, fenster)
    sim3 = simuliere(fein3["struktur"], imp3, ter3, netto3, soc3, einstand3, rt, 800)
    if not sim3["ok"]:
        fehler.append("Szenario 3: Simulator lehnt den Plan ab.")
    for h in (22, 23):
        if sim3["entl_exp"][h] > 1e-6:
            fehler.append("Szenario 3: Export in Stunde %d trotz Bonusfenster-Ende (%.2f kWh)."
                          % (h, sim3["entl_exp"][h]))
    if sim3["entl_haus"][22] < min(0.8, netto3[22]) - 1e-6:
        fehler.append("Szenario 3: Hausbedarf 22h nicht gedeckt (%.2f kWh)." % sim3["entl_haus"][22])

    # Szenario 4 (1.3.5): dieselbe Welt OHNE Saldering (ab 2027, oder Schalter
    # in HA aus). Der 06:48-Lauf vom 03.09. mit Einstand 33,4 ct liess im
    # Replay 2026-09-04 unter AUS 2,44 kWh den ganzen Tag liegen: ohne den um
    # 11 ct hoeheren Exportwert gab es keine Abend-Ankerbuchung, die der
    # Umschicht-Pass nach vorn ziehen konnte, und das Greedy-Gate hing am
    # bezahlten Einstand. Mit der Halte-Schwelle aus dem Reservationswert
    # (billigster Nachkauf des Tages) muss die Morgenstunde bedient werden und
    # der Akku am Tagesende leer sein. Gegenprobe 4b (kein billiger Nachkauf,
    # Mittag auf 0,19 geklemmt): Halten bleibt richtig, Stunde 7 bleibt leer.
    cfg_aus = dict(cfg)
    cfg_aus["saldering"] = False
    cfg_aus["rt"] = rt
    boden = KAP_KWH * BODEN_PCT / 100.0
    for variante, mittag in (("4", None), ("4b", 0.19)):
        b = list(beurs2)
        if mittag is not None:
            for h in range(8, 24):
                b[h] = max(b[h], mittag)
        imp4 = [x + 0.1327 for x in b]
        ter4 = [wert_terug(b[h], h, cfg_aus) for h in range(24)]
        pk4 = {"imp": list(imp4), "ter": list(ter4), "fehlt": [False] * 24}
        res4 = reservationswert(pk4, cfg_aus, fenster)
        for h in range(7):
            imp4[h] = 999.0
            ter4[h] = -999.0
        alt4 = optimiere(imp4, ter4, netto2, soc2, einstand2, rt, puffer, 800, fenster)
        alt4 = verbessere(alt4, imp4, ter4, netto2, soc2, einstand2, rt, puffer, 800, fenster)
        opt4 = optimiere(imp4, ter4, netto2, soc2, einstand2, rt, puffer, 800, fenster, res4)
        fein4 = verbessere(opt4, imp4, ter4, netto2, soc2, einstand2, rt, puffer, 800, fenster)
        sim4 = simuliere(fein4["struktur"], imp4, ter4, netto2, soc2, einstand2, rt, 800)
        entl7 = sim4["entl_haus"][7] + sim4["entl_exp"][7]
        if not sim4["ok"]:
            fehler.append("Szenario %s: Simulator lehnt den Plan ab." % variante)
        if abs(sim4["eur"] - fein4["eur"]) > 0.005:
            fehler.append("Szenario %s: Simulator-Bilanz weicht ab (%.4f vs %.4f)."
                          % (variante, sim4["eur"], fein4["eur"]))
        if fein4["eur"] < opt4["eur"] - 1e-6:
            fehler.append("Szenario %s: Umschicht-Pass verschlechtert die Bilanz." % variante)
        for h in range(24):
            if sim4["entl_haus"][h] + sim4["entl_exp"][h] > 0.8 + 1e-6:
                fehler.append("Szenario %s: Stunde %d ueber 800 W." % (variante, h))
            if not fenster[h] and sim4["entl_exp"][h] > 1e-6:
                fehler.append("Szenario %s: Export in Stunde %d ausserhalb des Bonusfensters."
                              % (variante, h))
        if mittag is None:
            if entl7 < 0.5:
                fehler.append("Szenario 4: Stunde 7 bleibt ohne Saldering leer (%.2f kWh), "
                              "Halte-Schwelle wirkt nicht." % entl7)
            if sim4["soc"][23] - boden > 0.3:
                fehler.append("Szenario 4: %.2f kWh bleiben ohne Saldering ungenutzt im Akku."
                              % (sim4["soc"][23] - boden))
            if fein4["eur"] < alt4["eur"] + 0.05:
                fehler.append("Szenario 4: Halte-Schwelle bringt keinen Gewinn gegen das "
                              "Einstand-Gate (%.4f vs %.4f)." % (fein4["eur"], alt4["eur"]))
        elif entl7 > 1e-6:
            fehler.append("Szenario 4b: Stunde 7 verkauft ohne billigen Nachkauf (%.2f kWh)."
                          % entl7)
    return fehler


def selbsttest():
    # Laeuft STILL: die Trade- und UMSCHICHTUNG-Zeilen der Testszenarien
    # gehoeren nicht ins Betriebslog (Befund 2026-09-03: fuenf Phantom-
    # Umschichtungen beim Start sahen aus wie ein echter Planlauf).
    global log
    echt = log
    log = lambda text: None
    try:
        return _selbsttest_kern()
    finally:
        log = echt


# ------------------------------------------------------- Plan bauen und pruefen
def plan_aktionen(opt, ab_stunde, entl_max_w):
    aktionen = []
    for h in range(24):
        akt = "RUHE"
        watt = 0
        if h < ab_stunde:
            akt = "VORBEI"
        else:
            lad_m = opt["lad_netz"][h] + opt["lad_pv"][h]
            ent_m = opt["entl_haus"][h] + opt["entl_exp"][h]
            if opt["lad_netz"][h] > 0.01:
                akt = "ZWANGSLADUNG"
                watt = min(LADEN_NETZ_MAX_W, max(250, round(lad_m * 1000 / 50) * 50))
            elif opt["entl_exp"][h] > 0.01:
                akt = "ZWANGSENTLADUNG"
                watt = min(entl_max_w, max(100, round(ent_m * 1000 / 50) * 50))
            elif opt["lad_pv"][h] > 0.01 or opt["entl_haus"][h] > 0.01:
                akt = "ANTI_FEED"
        aktionen.append({"h": h, "aktion": akt, "watt": int(watt)})
    return aktionen


def pruefe_invarianten(aktionen, opt, fehlt, ab_stunde, entl_max_w, soc_start_kwh):
    # Notbremse vor jedem Publish: bei Verstoss wird NICHT publiziert.
    fehler = []
    if len(aktionen) != 24:
        fehler.append("Plan hat %d statt 24 Stunden." % len(aktionen))
        return fehler
    for a in aktionen:
        if a["aktion"] == "ZWANGSENTLADUNG":
            if a["watt"] > min(ENTL_MAX_W_HART, entl_max_w) or a["watt"] < 100:
                fehler.append("Stunde %d: Entladung %d W ausserhalb 100..%d."
                              % (a["h"], a["watt"], min(ENTL_MAX_W_HART, entl_max_w)))
        if a["aktion"] == "ZWANGSLADUNG":
            if a["watt"] > LADEN_NETZ_MAX_W or a["watt"] < 250:
                fehler.append("Stunde %d: Ladung %d W ausserhalb 250..%d."
                              % (a["h"], a["watt"], LADEN_NETZ_MAX_W))
        if a["h"] >= ab_stunde and fehlt[a["h"]] and a["aktion"] not in ("RUHE", "VORBEI"):
            fehler.append("Stunde %d: Aktion %s trotz gesperrtem Preis."
                          % (a["h"], a["aktion"]))
    # Steht der Akku REAL unter dem Boden (z.B. 11.9 % nach einem Entladeabend),
    # ist das kein Planfehler: der Optimierer kann die Bahn nie unter den Start
    # druecken (Start-Entnahme ist bei start_frei=0 gesperrt, Paar-Trades kehren
    # zur Basis zurueck). Untergrenze ist darum min(Boden, Ist-Start).
    # Bug 1.0.0 (Nacht 29./30.08.): fester Boden blockierte 13 Laeufe in Folge.
    boden = min(KAP_KWH * BODEN_PCT / 100.0, soc_start_kwh)
    for h, s in enumerate(opt["soc"]):
        if s < boden - 1e-6 or s > KAP_KWH + 1e-6:
            fehler.append("SoC-Bahn Stunde %d ausserhalb der Grenzen (%.2f kWh)." % (h, s))
    if not math.isfinite(opt["eur"]):
        fehler.append("Tagesbilanz ist keine endliche Zahl.")
    return fehler


def publiziere_plan(plan_tag, aktionen, eur, saldering=None):
    payload = json.dumps({
        "datum": plan_tag.strftime("%Y-%m-%d"),
        "erzeugt": jetzt().strftime("%Y-%m-%d %H:%M:%S"),
        "eur": eur,
        "saldering": saldering,  # Regime, in dem der Plan gerechnet wurde (1.3.5)
        "stunden": aktionen,
    }, separators=(",", ":"))
    discovery = json.dumps({
        "name": "Batterie v2 Plan",
        "unique_id": "brainwiki_batterie_v2plan",
        "object_id": "batterie_v2_plan",
        "state_topic": PLAN_TOPIC_BASIS + "/state",
        "json_attributes_topic": PLAN_TOPIC_BASIS + "/attr",
        "icon": "mdi:battery-clock",
    }, separators=(",", ":"))
    dienst("mqtt", "publish", {"topic": "homeassistant/sensor/brainwiki_batterie_v2plan/config",
                               "payload": discovery, "retain": True})
    dienst("mqtt", "publish", {"topic": PLAN_TOPIC_BASIS + "/attr",
                               "payload": payload, "retain": True})
    dienst("mqtt", "publish", {"topic": PLAN_TOPIC_BASIS + "/state",
                               "payload": plan_tag.strftime("%Y-%m-%d"), "retain": True})


def publiziere_status(status, detail, eur=None, saldering=None):
    discovery = json.dumps({
        "name": "Batterie v2 Planner Status",
        "unique_id": "brainwiki_batterie_v2planner",
        "object_id": "batterie_v2_planner_status",
        "state_topic": STATUS_TOPIC_BASIS + "/state",
        "json_attributes_topic": STATUS_TOPIC_BASIS + "/attr",
        "icon": "mdi:calculator-variant",
    }, separators=(",", ":"))
    attr = json.dumps({"zeit": jetzt().strftime("%Y-%m-%d %H:%M:%S"),
                       "detail": detail, "eur_rest": eur, "saldering": saldering},
                      separators=(",", ":"))
    try:
        dienst("mqtt", "publish", {"topic": "homeassistant/sensor/brainwiki_batterie_v2planner/config",
                                   "payload": discovery, "retain": True})
        dienst("mqtt", "publish", {"topic": STATUS_TOPIC_BASIS + "/attr",
                                   "payload": attr, "retain": True})
        dienst("mqtt", "publish", {"topic": STATUS_TOPIC_BASIS + "/state",
                                   "payload": status, "retain": True})
    except Exception as e:
        log("WARNUNG: Status-Publish fehlgeschlagen: %s" % e)


def vergangene_stunden_uebernehmen(aktionen, plan_tag, ab_stunde):
    # Stunden vor ab_stunde tragen die Aktion des zuletzt publizierten Plans
    # desselben Tages statt VORBEI. Befund 2026-09-03 19:31: nach dem
    # Add-on-Neustart um 19:15 stand die laufende Stunde 19 als VORBEI im
    # Sensor; der Failsafe hielt die laufende ZWANGSENTLADUNG fuer ungeplant
    # und raeumte sie ab, Andre stellte sie von Hand wieder her. Der Executor
    # ist gegen die uebernommenen Aktionen idempotent (prueft den Ist-Zustand).
    letzter = lade_json(LAST_PLAN_PATH, None)
    if not letzter or letzter.get("datum") != plan_tag.strftime("%Y-%m-%d"):
        return aktionen
    alte = {a["h"]: a for a in letzter.get("stunden", []) if "h" in a}
    neu = []
    for a in aktionen:
        alt = alte.get(a["h"])
        if a["h"] < ab_stunde and alt and alt.get("aktion") not in (None, "VORBEI"):
            neu.append({"h": a["h"], "aktion": alt["aktion"], "watt": int(alt.get("watt", 0))})
        else:
            neu.append(a)
    return neu


def plan_unveraendert(plan_tag, aktionen, ab_stunde, saldering=None):
    letzter = lade_json(LAST_PLAN_PATH, None)
    if not letzter or letzter.get("datum") != plan_tag.strftime("%Y-%m-%d"):
        return False
    if letzter.get("saldering") != saldering:
        return False  # Regimewechsel: publizieren, auch wenn die Aktionen gleich bleiben
    alte = {a["h"]: (a["aktion"], a["watt"]) for a in letzter.get("stunden", [])}
    for a in aktionen:
        if a["h"] < ab_stunde:
            continue
        if alte.get(a["h"]) != (a["aktion"], a["watt"]):
            return False
    return True


# --------------------------------------------------------------- Planungslaeufe
def rechne_und_publiziere(plan_tag, ab_stunde, pv_entity, pv_feld, state, opts, anlass):
    cfg = lese_konfig()
    karte = beurs_karte()
    karte_ne = ne_karte()
    offset = einkaufs_offset(karte, cfg)
    pk = preis_kurven(plan_tag, karte, offset, cfg, karte_ne)

    fehlt_rest = sum(1 for h in range(ab_stunde, 24) if pk["fehlt"][h])
    if fehlt_rest > 0:
        detail = ("%s: %d Beurs-Stunden ab %02d:00 fehlen, kein neuer Plan "
                  "(letzter gueltiger Plan bleibt stehen)." % (anlass, fehlt_rest, ab_stunde))
        log("WARNUNG: " + detail)
        publiziere_status("warnung", detail, None, cfg["saldering"])
        return

    profil = haus_profil(state, int(opts.get("profil_tage", 14)))
    pv = solcast_stunden(pv_entity, plan_tag, pv_feld)
    netto = [0.0] * 24
    for h in range(24):
        if h < ab_stunde:
            netto[h] = 0.0
            pk["imp"][h] = 999.0
            pk["ter"][h] = -999.0
        else:
            netto[h] = round(profil[h] - pv[h], 4)

    if "einstand" not in state:
        state["einstand"] = float(opts.get("einstand_start", 0.15))
    einstand = einstand_pflegen(state, cfg, karte, offset, karte_ne)

    soc_pct = zahl("sensor.marstek_venus_modbus_soc_batterie", 50)
    soc_start_kwh = KAP_KWH * soc_pct / 100.0
    # Export aus dem Akku nur im Zonnebonus-Fenster (Andre 2026-09-03), sonst
    # deckt die Entladung nur den Hausbedarf (anti_feed = Nulleinspeisung).
    export_ok = [cfg["venster_von"] <= h < cfg["venster_bis"] for h in range(24)]
    # Halte-Schwelle fuer Startinhalt (1.3.5): Folgetag als Referenz, sobald
    # seine Beurs-Kurve vollstaendig da ist (ab ~13 Uhr), sonst der geplante Tag
    # selbst mit voller Kurve (auch die vergangenen Stunden) als Naeherung.
    ref_tag = plan_tag + timedelta(days=1)
    ref_name = "Folgetag"
    pk_ref = preis_kurven(ref_tag, karte, offset, cfg, karte_ne)
    if any(pk_ref["fehlt"]):
        pk_ref = preis_kurven(plan_tag, karte, offset, cfg, karte_ne)
        ref_name = "Tageskurve"
    reservation = reservationswert(pk_ref, cfg, export_ok)
    if reservation is None:
        reservation = einstand
        ref_name = "Einstand"
    opt = optimiere(pk["imp"], pk["ter"], netto, soc_start_kwh, einstand,
                    cfg["rt"], cfg["puffer"], cfg["entl_max_w"], export_ok, reservation)
    try:
        opt = verbessere(opt, pk["imp"], pk["ter"], netto, soc_start_kwh, einstand,
                         cfg["rt"], cfg["puffer"], cfg["entl_max_w"], export_ok)
    except Exception as e:
        log("WARNUNG: Umschicht-Pass abgebrochen (%s), Greedy-Plan gilt." % e)
    aktionen = plan_aktionen(opt, ab_stunde, cfg["entl_max_w"])

    fehler = pruefe_invarianten(aktionen, opt, pk["fehlt"], ab_stunde, cfg["entl_max_w"], soc_start_kwh)
    if fehler:
        detail = "%s: Invarianten verletzt, Plan NICHT publiziert: %s" % (anlass, " | ".join(fehler))
        log("FEHLER: " + detail)
        melde("Batterie-Planer: Plan verworfen", detail)
        publiziere_status("fehler", detail, None, cfg["saldering"])
        return

    log("%s %s ab %02d:00: %+.2f EUR Restbilanz, %d Trades (SoC %.1f %%, Einstand %.1f ct, "
        "Halte-Schwelle %.1f ct aus %s, Saldering %s)"
        % (anlass, plan_tag.strftime("%Y-%m-%d"), ab_stunde, opt["eur"],
           len(opt["trades"]), soc_pct, einstand * 100, reservation * 100, ref_name,
           "an" if cfg["saldering"] else "aus"))
    for t in opt["trades"]:
        log("  " + t)

    if plan_unveraendert(plan_tag, aktionen, ab_stunde, cfg["saldering"]):
        log("Plan unveraendert, kein Publish (Flatter-Bremse).")
        publiziere_status("ok", "%s: Plan unveraendert." % anlass, opt["eur"], cfg["saldering"])
        return

    aktionen = vergangene_stunden_uebernehmen(aktionen, plan_tag, ab_stunde)
    publiziere_plan(plan_tag, aktionen, opt["eur"], cfg["saldering"])
    schreibe_json(LAST_PLAN_PATH, {"datum": plan_tag.strftime("%Y-%m-%d"),
                                   "saldering": cfg["saldering"], "stunden": aktionen})
    aktiv = sum(1 for a in aktionen if a["aktion"] not in ("RUHE", "VORBEI"))
    log("PLAN VEROEFFENTLICHT: %s, %d aktive Stunden -> sensor.batterie_v2_plan"
        % (plan_tag.strftime("%Y-%m-%d"), aktiv))
    publiziere_status("ok", "%s: Plan publiziert (%d aktive Stunden)." % (anlass, aktiv),
                      opt["eur"], cfg["saldering"])


def plan_heute(state, opts):
    nun = jetzt()
    tag0 = nun.replace(hour=0, minute=0, second=0, microsecond=0)
    ab = nun.hour + 1  # laufende Stunde ist eingefroren
    if ab >= 24:
        log("PlanHeute: Tag vorbei, nichts mehr zu planen.")
        return
    if ist_dst_tag(tag0):
        log("WARNUNG: DST-Umstelltag, kein Tagesplan.")
        publiziere_status("warnung", "DST-Umstelltag, kein Plan fuer heute.")
        return
    rechne_und_publiziere(tag0, ab, "sensor.solcast_pv_forecast_prognose_heute",
                          "pv_estimate", state, opts, "PLAN HEUTE")


def plan_morgen(state, opts):
    tag0 = (jetzt() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if ist_dst_tag(tag0):
        log("WARNUNG: Morgen ist DST-Umstelltag, kein Folgetagsplan.")
        publiziere_status("warnung", "DST-Umstelltag morgen, kein Folgetagsplan.")
        return
    rechne_und_publiziere(tag0, 0, "sensor.solcast_pv_forecast_prognose_morgen",
                          "pv_estimate", state, opts, "PLAN MORGEN")


# ------------------------------------------------------------------- Hauptloop
def main():
    global TZ
    fehler = selbsttest()
    if fehler:
        for f in fehler:
            log("SELBSTTEST FEHLGESCHLAGEN: %s" % f)
        raise SystemExit("Selbsttest fehlgeschlagen, Add-on stoppt OHNE Publish "
                         "(alter retained Plan bleibt stehen).")
    log("Selbsttest bestanden (5 Szenarien: Block-Bewertung, Sunk-Cost-Dreieck, Gegenprobe ohne "
        "billigen Nachkauf, Bonusfenster, Halte-Schwelle ohne Saldering mit Gegenprobe).")
    opts = lade_json(OPTIONS_PATH, {})
    takt = int(opts.get("takt_minute", 55))
    profil_tage = int(opts.get("profil_tage", 14))

    for versuch in range(30):
        try:
            tz_name = api_call("/config")["time_zone"]
            TZ = ZoneInfo(tz_name)
            log("Zeitzone: %s" % tz_name)
            break
        except Exception as e:
            log("WARNUNG: HA-API noch nicht erreichbar (%s), warte 10 s." % e)
            time.sleep(10)
    if TZ is None:
        raise SystemExit("HA-API nicht erreichbar, Abbruch.")

    log("Batterie-Planer v2 gestartet (Takt hh:%02d, Einstand %.2f, Profil %d Tage)."
        % (takt, float(opts.get("einstand_start", 0.15)), profil_tage))

    state = lade_json(STATE_PATH, {"tage": {}})
    if "tage" not in state:
        state = {"tage": {}}
    try:
        state = profil_auffuellen(state, profil_tage)
    except Exception as e:
        log("WARNUNG: Profil-Aufbau fehlgeschlagen (%s), Notnagel-Profil aktiv." % e)

    # Erster Lauf sofort, danach stuendlich um hh:takt.
    try:
        plan_heute(state, opts)
    except Exception as e:
        log("FEHLER PlanHeute (Start): %s" % e)
        melde("Batterie-Planer: Fehler", "Startlauf fehlgeschlagen: %s" % e)
        publiziere_status("fehler", "Startlauf: %s" % e)

    while True:
        nun = jetzt()
        ziel = nun.replace(minute=takt, second=0, microsecond=0)
        if ziel <= nun:
            ziel += timedelta(hours=1)
        time.sleep(max(5.0, (ziel - nun).total_seconds()))
        try:
            opts = lade_json(OPTIONS_PATH, opts)
            lauf = jetzt()
            if lauf.hour == 1:
                state = lade_json(STATE_PATH, state)
                state = profil_auffuellen(state, profil_tage)
            plan_heute(state, opts)
            if lauf.hour == 23:
                plan_morgen(state, opts)
        except Exception as e:
            log("FEHLER im Stundenlauf: %s" % e)
            melde("Batterie-Planer: Fehler", "Stundenlauf fehlgeschlagen: %s" % e)
            publiziere_status("fehler", "Stundenlauf: %s" % e)


if __name__ == "__main__":
    main()
