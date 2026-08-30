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
    "dis": "sensor.batterij_ontladen_kwh",
    "lnet": "sensor.batterij_laden_uit_net_kwh",
    "lpv": "sensor.batterij_laden_uit_pv_kwh",
}

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
    try:
        cfg["saldering"] = (zustand("input_boolean.ne_saldering_actief")["state"] == "on")
    except Exception:
        cfg["saldering"] = True
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


def einkaufs_offset(karte):
    # NaN-Logik der PS-Fassung: ein legitim negativer Live-Preis darf nicht in
    # den 0.14-Fallback kippen, nur ein fehlender Wert.
    huidige = zahl("sensor.next_energy_huidige_prijs", float("nan"))
    schluessel = jetzt().strftime("%Y-%m-%d %H")
    if (not math.isnan(huidige)) and schluessel in karte:
        return huidige - karte[schluessel]
    return 0.14


def preis_kurven(tag0, karte, offset, cfg):
    # imp/ter je Stunde; fehlende Beurs-Stunden werden mit Sperr-Preisen
    # (999/-999) vom Handel ausgeschlossen statt als Phantom-0 gerechnet.
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
        imp[h] = beurs[h] + offset
        bonus = 0.0
        if cfg["venster_von"] <= h < cfg["venster_bis"] and beurs[h] > 0:
            bonus = cfg["bonus_pct"] * beurs[h] * cfg["v_faktor"]
        bel = cfg["belasting"] if cfg["saldering"] else 0.0
        ter[h] = round(beurs[h] - cfg["verkoop"] + bonus + bel, 4)
    return {"imp": imp, "ter": ter, "beurs": beurs, "fehlt": fehlt}


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
    lnet = deltas(kanten["lnet"])
    lpv = deltas(kanten["lpv"])
    pv_punkte = serie("sensor.solcast_pv_forecast_aktuelle_leistung",
                      tag0, tag0 + timedelta(hours=24))
    pv_ist = integriere_stunden(pv_punkte, tag0)
    haus = [0.0] * 24
    for h in range(24):
        netto = imp[h] - exp[h] + dis[h] - lnet[h] - lpv[h]
        haus[h] = round(netto + pv_ist[h], 4)
    return haus


def profil_auffuellen(state, profil_tage):
    # Fehlende volle Tage (gestern rueckwaerts) aus der HA-Historie nachrechnen.
    # Recorder haelt ~10 Tage; nicht rechenbare Tage werden als leer markiert.
    heute0 = jetzt().replace(hour=0, minute=0, second=0, microsecond=0)
    neu = 0
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


def haus_profil(state):
    # Median je Stunde ueber die gesammelten Tage; Notnagel 350 W Grundlast.
    profil = [0.35] * 24
    for h in range(24):
        werte = []
        for eintrag in state["tage"].values():
            haus = eintrag.get("haus")
            if haus is None:
                continue
            anz = int(eintrag.get("stunden", 24))
            if h < anz:
                werte.append(float(haus[h]))
        if werte:
            werte.sort()
            profil[h] = werte[(len(werte) - 1) // 2]
    return profil


# ------------------------------------------------------------------ Optimierer
def optimiere(imp, ter, netto, soc_start_kwh, einstand_start, rt, puffer, entl_max_w):
    # Voll-dynamischer, verlustfreier Stunden-Optimierer (greedy best-pair).
    # 1:1-Port aus batterie_schatten.ps1; Eingaben in Meter-kWh, SoC-Bahn in
    # gespeicherten kWh.
    n = len(netto)
    sqrt_rt = math.sqrt(rt)
    boden = KAP_KWH * BODEN_PCT / 100.0
    entl_max = entl_max_w / 1000.0
    lad_netz = LADEN_NETZ_MAX_W / 1000.0
    lad_pv = LADEN_PV_MAX_W / 1000.0

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

    def richtung_ok(h, soll):
        laedt = (lad_netz_m[h] + lad_pv_m[h]) > 1e-9
        entlaedt = (entl_haus[h] + entl_exp[h]) > 1e-9
        return (not entlaedt) if soll == "laden" else (not laedt)

    for _ in range(MAX_ITER):
        best_marge = puffer - 1e-12
        best = None
        for k in range(n):
            if not richtung_ok(k, "entladen"):
                continue
            entl_rest = entl_max - (entl_haus[k] + entl_exp[k])
            if entl_rest <= 1e-9:
                continue
            for senke in ("haus", "export"):
                if senke == "haus" and bedarf[k] <= 1e-9:
                    continue
                # Solange die Stunde Netzbezug hat, wirkt Entladung am nettieren-
                # den P1 nur als Import-Minderung; echter Export erst danach.
                if senke == "export" and bedarf[k] > 1e-9:
                    continue
                wert = imp[k] if senke == "haus" else ter[k]
                # --- Quelle Startinhalt (jederzeit verfuegbar) ---
                if start_frei > 1e-9:
                    m = sqrt_rt * wert - einstand_start
                    if m > best_marge:
                        frei = start_frei
                        for t in range(k, n):
                            frei = min(frei, soc[t] - boden)
                        meter = min(frei * sqrt_rt, entl_rest)
                        if senke == "haus":
                            meter = min(meter, bedarf[k])
                        if meter > 1e-6:
                            best_marge = m
                            best = {"art": "start", "s": -1, "k": k,
                                    "senke": senke, "meter": meter, "marge": m}
                # --- Quellen Netz / PV in frueherer Stunde ---
                for s in range(k):
                    if not richtung_ok(s, "laden"):
                        continue
                    for quelle in ("netz", "pv"):
                        if quelle == "pv" and pv_frei[s] <= 1e-9:
                            continue
                        kost = imp[s] if quelle == "netz" else ter[s]
                        m = rt * wert - kost
                        if m <= best_marge:
                            continue
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
                        meter = min(lad_rest, kopf / sqrt_rt, entl_rest / rt)
                        if senke == "haus":
                            meter = min(meter, bedarf[k] / rt)
                        if meter > 1e-6:
                            best_marge = m
                            best = {"art": quelle, "s": s, "k": k,
                                    "senke": senke, "meter": meter, "marge": m}
        if best is None:
            break
        k = best["k"]
        senke = best["senke"]
        if best["art"] == "start":
            st = best["meter"] / sqrt_rt
            start_frei -= st
            for t in range(k, n):
                soc[t] -= st
            raus = best["meter"]
            trades.append("Startinhalt %.2f kWh -> %s %02dh (%.1f ct Marge/kWh)"
                          % (raus, senke, k, best["marge"] * 100))
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
            raus = best["meter"] * rt
            trades.append("%s %02dh %.2f kWh -> %s %02dh (%.1f ct Marge/kWh)"
                          % (best["art"], s, best["meter"], senke, k, best["marge"] * 100))
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
            "trades": trades}


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


def publiziere_plan(plan_tag, aktionen, eur):
    payload = json.dumps({
        "datum": plan_tag.strftime("%Y-%m-%d"),
        "erzeugt": jetzt().strftime("%Y-%m-%d %H:%M:%S"),
        "eur": eur,
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


def publiziere_status(status, detail, eur=None):
    discovery = json.dumps({
        "name": "Batterie v2 Planner Status",
        "unique_id": "brainwiki_batterie_v2planner",
        "object_id": "batterie_v2_planner_status",
        "state_topic": STATUS_TOPIC_BASIS + "/state",
        "json_attributes_topic": STATUS_TOPIC_BASIS + "/attr",
        "icon": "mdi:calculator-variant",
    }, separators=(",", ":"))
    attr = json.dumps({"zeit": jetzt().strftime("%Y-%m-%d %H:%M:%S"),
                       "detail": detail, "eur_rest": eur}, separators=(",", ":"))
    try:
        dienst("mqtt", "publish", {"topic": "homeassistant/sensor/brainwiki_batterie_v2planner/config",
                                   "payload": discovery, "retain": True})
        dienst("mqtt", "publish", {"topic": STATUS_TOPIC_BASIS + "/attr",
                                   "payload": attr, "retain": True})
        dienst("mqtt", "publish", {"topic": STATUS_TOPIC_BASIS + "/state",
                                   "payload": status, "retain": True})
    except Exception as e:
        log("WARNUNG: Status-Publish fehlgeschlagen: %s" % e)


def plan_unveraendert(plan_tag, aktionen, ab_stunde):
    letzter = lade_json(LAST_PLAN_PATH, None)
    if not letzter or letzter.get("datum") != plan_tag.strftime("%Y-%m-%d"):
        return False
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
    offset = einkaufs_offset(karte)
    pk = preis_kurven(plan_tag, karte, offset, cfg)

    fehlt_rest = sum(1 for h in range(ab_stunde, 24) if pk["fehlt"][h])
    if fehlt_rest > 0:
        detail = ("%s: %d Beurs-Stunden ab %02d:00 fehlen, kein neuer Plan "
                  "(letzter gueltiger Plan bleibt stehen)." % (anlass, fehlt_rest, ab_stunde))
        log("WARNUNG: " + detail)
        publiziere_status("warnung", detail)
        return

    profil = haus_profil(state)
    pv = solcast_stunden(pv_entity, plan_tag, pv_feld)
    netto = [0.0] * 24
    for h in range(24):
        if h < ab_stunde:
            netto[h] = 0.0
            pk["imp"][h] = 999.0
            pk["ter"][h] = -999.0
        else:
            netto[h] = round(profil[h] - pv[h], 4)

    soc_pct = zahl("sensor.marstek_venus_modbus_soc_batterie", 50)
    soc_start_kwh = KAP_KWH * soc_pct / 100.0
    opt = optimiere(pk["imp"], pk["ter"], netto, soc_start_kwh,
                    float(opts.get("einstand_start", 0.15)),
                    cfg["rt"], cfg["puffer"], cfg["entl_max_w"])
    aktionen = plan_aktionen(opt, ab_stunde, cfg["entl_max_w"])

    fehler = pruefe_invarianten(aktionen, opt, pk["fehlt"], ab_stunde, cfg["entl_max_w"], soc_start_kwh)
    if fehler:
        detail = "%s: Invarianten verletzt, Plan NICHT publiziert: %s" % (anlass, " | ".join(fehler))
        log("FEHLER: " + detail)
        melde("Batterie-Planer: Plan verworfen", detail)
        publiziere_status("fehler", detail)
        return

    log("%s %s ab %02d:00: %+.2f EUR Restbilanz, %d Trades (SoC %.1f %%)"
        % (anlass, plan_tag.strftime("%Y-%m-%d"), ab_stunde, opt["eur"],
           len(opt["trades"]), soc_pct))
    for t in opt["trades"]:
        log("  " + t)

    if plan_unveraendert(plan_tag, aktionen, ab_stunde):
        log("Plan unveraendert, kein Publish (Flatter-Bremse).")
        publiziere_status("ok", "%s: Plan unveraendert." % anlass, opt["eur"])
        return

    publiziere_plan(plan_tag, aktionen, opt["eur"])
    schreibe_json(LAST_PLAN_PATH, {"datum": plan_tag.strftime("%Y-%m-%d"), "stunden": aktionen})
    aktiv = sum(1 for a in aktionen if a["aktion"] not in ("RUHE", "VORBEI"))
    log("PLAN VEROEFFENTLICHT: %s, %d aktive Stunden -> sensor.batterie_v2_plan"
        % (plan_tag.strftime("%Y-%m-%d"), aktiv))
    publiziere_status("ok", "%s: Plan publiziert (%d aktive Stunden)." % (anlass, aktiv), opt["eur"])


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
