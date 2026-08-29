# ha-batterie-planner

Home-Assistant-Add-on-Repository mit einem Add-on: **Batterie-Planer v2**.

Der Planer rechnet stuendlich einen gewinnmaximalen, verlustfreien Stunden-Fahrplan
fuer eine Marstek-Venus-Heimbatterie (rolling horizon) und publiziert ihn retained
per MQTT (`brainwiki/batterie/v2plan`). Ausgefuehrt wird der Plan von einer
schlanken Executor-Automation in Home Assistant; der Planer selbst steuert nichts
direkt.

Eingangsdaten (alles Entitaeten in Home Assistant): Beurs-Preiskurve (EnergyZero,
48 h), Live-Preis-Offset, Solcast-PV-Prognose, Ist-SoC der Batterie sowie ein
selbst aufgebautes Median-Hauslastprofil aus der Recorder-Historie. Das
NextEnergy-Wertmodell (Zonnebonus, Saldering, Energiebelasting, Verkoopvergoeding)
kommt live aus `input_`-Helfern, Regelaenderungen sind reine Config-Flips.

Installation: dieses Repository unter *Einstellungen > Add-ons > Add-on-Store >
Repositories* eintragen, dann "Batterie-Planer v2" installieren.

Hinweis: Die Entitaets-IDs sind auf eine konkrete Installation zugeschnitten
(siehe `planner.py`, Abschnitt Konstanten/ZAEHLER); fuer andere Haushalte muessen
sie angepasst werden.
