# Batterie-Planer v2

Stuendlicher Batterie-Fahrplan (rolling horizon) fuer die Marstek-v2-Steuerung.

- Jede Stunde um Minute `takt_minute` (Default :55) wird der Rest des Tages neu
  geplant: Ist-SoC, Rest-Preise, frisches Solcast. Um 23:takt zusaetzlich der
  Folgetag.
- Die laufende Stunde ist eingefroren, publiziert wird nur bei echter
  Planaenderung (Flatter-Bremse).
- Notbremse: verletzt ein Plan die harten Invarianten (Entladung ueber 800 W,
  Ladung ausserhalb 250..2000 W, Handel auf gesperrten Preisstunden, SoC-Bahn
  ausserhalb der Grenzen), wird NICHT publiziert, nur gemeldet; der letzte
  gueltige Plan bleibt retained stehen.
- Status-Sensor: `sensor.batterie_v2_planner_status` (ok / warnung / fehler,
  Details in den Attributen).

## Optionen

| Option | Default | Bedeutung |
|---|---|---|
| `einstand_start` | 0.15 | EUR/kWh Startwert; danach fuehrt das Add-on den echten gewichteten Einkaufspreis der gespeicherten Energie selbst im State mit |
| `profil_tage` | 14 | Tage im Median-Hauslastprofil |
| `takt_minute` | 55 | Minute des stuendlichen Planungslaufs |
