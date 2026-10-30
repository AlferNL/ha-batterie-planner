# Batterie-Planer v2

Stuendlicher Batterie-Fahrplan (rolling horizon) fuer die Marstek-v2-Steuerung.

- Jede Stunde um Minute `takt_minute` (Default :55) wird der Rest des Tages neu
  geplant: Ist-SoC, Rest-Preise, frisches Solcast. Die laufende Stunde ist
  eingefroren, publiziert wird nur bei echter Planaenderung (Flatter-Bremse).
- Um 00:00:30 rechnet ein eigener Tageslauf den vollen neuen Tag mit echtem
  SoC (seit 1.4.0). Der Executor zieht auf den Sensorwechsel und um hh:01.
- Folgetag-Vorschau (seit 1.4.0): sobald die Beurs-Kurve fuer morgen
  vollstaendig ist (ab ~13:30), rechnet jeder Stundenlauf zusaetzlich den
  Folgetag auf ein zweites retained Topic (`sensor.batterie_v2_plan_morgen`),
  mit dem prognostizierten Tagesend-SoC des Heute-Plans als Start. Jede
  Solcast-Aktualisierung ist damit spaetestens eine Stunde spaeter drin.
  Kein fester Zeitpunkt, die Daten selbst loesen aus. Die Vorschau ist reine
  Anzeige: das Haus faehrt nur `sensor.batterie_v2_plan`.
- Notbremse: verletzt ein Plan die harten Invarianten (Entladung ueber 800 W,
  Ladung ausserhalb 250 W bis B1 max. Ladeleistung, Handel auf gesperrten Preisstunden, SoC-Bahn
  ausserhalb der Grenzen), wird NICHT publiziert, nur gemeldet; der letzte
  gueltige Plan bleibt retained stehen.
- Status-Sensor: `sensor.batterie_v2_planner_status` (ok / warnung / fehler,
  Details in den Attributen). Die Vorschau fasst den Status nicht an.

## Sensoren

| Sensor | Topic | Bedeutung |
|---|---|---|
| `sensor.batterie_v2_plan` | `brainwiki/batterie/v2plan` | Hauptplan des laufenden Tages, wird vom Executor gefahren (Datum muss heute sein) |
| `sensor.batterie_v2_plan_morgen` | `brainwiki/batterie/v2plan_morgen` | Folgetag-Vorschau, Attribute `start_soc_pct` und `start_soc_quelle` (Heute-Plan-Ende oder live) |
| `sensor.batterie_v2_planner_status` | `brainwiki/batterie/v2planner` | Laufstatus des Hauptplans |

## Option

| Option | Default | Bedeutung |
|---|---|---|
| `einstand_start` | 0.15 | EUR/kWh Startwert; danach fuehrt das Add-on den echten gewichteten Einkaufspreis der gespeicherten Energie selbst im State mit |

## Regler in Home Assistant (seit 1.7.0)

Die uebrigen Einstellungen liest der Planer in jedem Lauf aus HA, sie stehen im
Config-Dashboard (pv-prognose, Ansicht Konfig, Abschnitt Batterie-Planer v2) und
wirken ab dem naechsten Lauf ohne Neustart. Ist ein Helfer unlesbar, gilt sein
letzter gelesener Wert, beim ersten Lauf der Default (mit WARNUNG im Log).

| Entity | Default | Bedeutung |
|---|---|---|
| `input_number.planer_warte_aufschlag` | 0.3 | Warte-Aufschlag (seit 1.5.0) in ct/kWh je Stunde: unter den PV-Stunden fuer dieselbe Entladung gewinnt die frueheste, eine spaetere nur, wenn sie je Stunde Abstand mehr als diesen Wert billiger ist. 0 = Reihenfolge wie bis 1.4.0 |
| `input_number.planer_profiltage` | 14 | Tage im Median-Hauslastprofil (die juengsten Tage mit Daten; 6 weitere bleiben nur als Vorrat im State). Eine Aenderung fuellt den Vorrat sofort nach |
| `input_number.planer_takt_minute` | 55 | Minute des stuendlichen Planungslaufs; eine Aenderung greift ab dem uebernaechsten Lauf |
| `number.marstek_venus_modbus_maximale_ladeleistung` | 2000 (unlesbar) | Hoechste Zwangsladung aus dem Netz je Stunde in W, wie bei der Entladung der Wert der Marstek selbst (Dashboard: B1 max. Ladeleistung), geklemmt auf 250 bis 2500. Mehr Leistung fuellt den Akku in einem kurzen Preistal weiter (zwei Stunden mit 2000 W rund 80 %, mit 2500 W voll) |
