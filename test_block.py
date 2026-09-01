#!/usr/bin/env python3
# A/B-Test Block-Bewertung 1.2.0 gegen 1.1.0 (Szenarien vom 2026-09-01).
# Aufruf im Repo-Root: python test_block.py
# Holt sich die alte 1.1.0-Fassung selbst aus der Git-Historie (d38728b).
# Exit-Code 0 = alles gruen, 1 = mindestens ein Check rot.
import importlib.util
import os
import subprocess
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
NEU_PFAD = os.path.join(HIER, "batterie_planner", "planner.py")
ALT_PFAD = os.path.join(HIER, "planner_old_gen.py")
ALT_COMMIT = "d38728b"  # 1.1.0


def lade(name, pfad):
    spec = importlib.util.spec_from_file_location(name, pfad)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if not os.path.exists(ALT_PFAD):
    quelle = subprocess.run(
        ["git", "-C", HIER, "show", ALT_COMMIT + ":batterie_planner/planner.py"],
        capture_output=True, text=True, check=True).stdout
    with open(ALT_PFAD, "w", encoding="utf-8") as f:
        f.write(quelle)

alt = lade("planner_alt", ALT_PFAD)
neu = lade("planner_neu", NEU_PFAD)

BEURS = [0.21, 0.19, 0.19, 0.18, 0.18, 0.19, 0.23, 0.25, 0.23, 0.19, 0.16,
         0.11, 0.06, 0.04, 0.05, 0.06, 0.12, 0.17, 0.22, 0.25, 0.28, 0.27,
         0.24, 0.22]
CFG = {"bonus_pct": 0.5, "v_faktor": 1.0, "venster_von": 6, "venster_bis": 22,
       "belasting": 0.1108, "verkoop": 0.0, "saldering": True}
RT, PUFFER = 0.85, 0.01


def kurven(mod, ab):
    imp = [b + 0.131 for b in BEURS]
    ter = [mod.wert_terug(BEURS[h], h, CFG) for h in range(24)]
    netto = [0.0] * 24
    netto[8] = 0.42
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
    for h in range(ab):
        imp[h] = 999.0
        ter[h] = -999.0
        netto[h] = 0.0
    return imp, ter, netto


def lauf(mod, ab, soc_pct, einstand):
    imp, ter, netto = kurven(mod, ab)
    soc = soc_pct / 100.0 * mod.KAP_KWH
    opt = mod.optimiere(imp, ter, netto, soc, einstand, RT, PUFFER, 800)
    fein = mod.verbessere(opt, imp, ter, netto, soc, einstand, RT, PUFFER, 800)
    sim = mod.simuliere(fein["struktur"], imp, ter, netto, soc, einstand, RT, 800)
    entl = [round(sim["entl_haus"][h] + sim["entl_exp"][h], 3) for h in range(24)]
    return {"eur": sim["eur"], "ok": sim["ok"], "entl": entl}


FEHLER = 0


def check(name, bedingung, detail=""):
    global FEHLER
    status = "OK  " if bedingung else "FAIL"
    if not bedingung:
        FEHLER += 1
    print("%s %s %s" % (status, name, detail))


for szenario, ab, soc_pct in (("A 07:43", 8, 53.4), ("B 08:55", 9, 38.7)):
    a = lauf(alt, ab, soc_pct, 0.15)
    n = lauf(neu, ab, soc_pct, 0.15)
    print("--- Szenario %s (alt %.4f EUR, neu %.4f EUR)" % (szenario, a["eur"], n["eur"]))
    print("    alt entl 8-11:", a["entl"][8:12], " neu entl 8-11:", n["entl"][8:12])
    check("Simulator ok (neu)", n["ok"])
    check("neu >= alt", n["eur"] >= a["eur"] - 1e-6, "(%+.4f EUR)" % (n["eur"] - a["eur"]))
    check("Stunde 9 voller Block (neu)", n["entl"][9] >= 0.7, str(n["entl"][9]))
    check("kein Stundenlimit verletzt", max(n["entl"]) <= 0.8 + 1e-6)

# Regression: reichlich Budget, beide sollten aehnlich enden
a = lauf(alt, 8, 100.0, 0.15)
n = lauf(neu, 8, 100.0, 0.15)
print("--- Regression voll (alt %.4f, neu %.4f)" % (a["eur"], n["eur"]))
check("Simulator ok (neu)", n["ok"])
check("neu >= alt - 1ct", n["eur"] >= a["eur"] - 0.01)

st = neu.selbsttest()
check("Selbsttest im Add-on leer", not st, "; ".join(st))

print("\n%d Fehler." % FEHLER)
sys.exit(1 if FEHLER else 0)
