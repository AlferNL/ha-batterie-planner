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
  Ladung ausserhalb 250..2000 W, Handel auf gesperrten Preisstunden, SoC-Bahn
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

## Optionen

| Option | Default | Bedeutung |
|---|---|---|
| `einstand_start` | 0.15 | EUR/kWh Startwert; danach fuehrt das Add-on den echten gewichteten Einkaufspreis der gespeicherten Energie selbst im State mit |
| `profil_tage` | 14 | Tage im Median-Hauslastprofil (die juengsten Tage mit Daten; 6 weitere bleiben nur als Vorrat im State) |
| `takt_minute` | 55 | Minute des stuendlichen Planungslaufs |
