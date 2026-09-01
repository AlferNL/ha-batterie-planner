# Changelog

## 1.2.0 (2026-09-01)

- **Block-Bewertung statt haus/export-Trennung:** Greedy und Umschicht-Pass
  bewerten eine Entladestunde jetzt als EINEN Block (Hausbedarf zuerst zum
  Einkaufspreis, Rest als Export zum Teruglever-Wert, gemischter kWh-Wert).
  Vorher konnte eine Stunde mit kleinem Rest-Hausbedarf nie einen vollen
  800-W-Block gewinnen: Export war erst nach Haus-Deckung erlaubt, und der
  Haus-Kruemel verlor als Einzelzug gegen volle Export-Bloecke spaeterer,
  schlechterer Stunden. Befund 2026-09-01: der 08:55-Replan liess Stunde 9
  (21,5 ct Marge, 0,14 kWh Rest-Hausbedarf) leer und verkaufte stattdessen
  in Stunde 10 (17,3 ct) und 11 (10,4 ct); der 07:43-Plan gab Stunde 10 den
  vollen Block und Stunde 9 die Reste. Die physische Regel bleibt: Haus
  zuerst, dann Export (nettierender P1); der unabhaengige Simulator prueft
  sie weiterhin je Stunde.
- **Selbsttest beim Start:** das 08:55-Szenario ist als Regressionstest fest
  eingebaut und laeuft bei jedem Add-on-Start (lokal, < 1 s). Schlaegt er
  fehl, stoppt das Add-on OHNE Publish; der alte retained Plan bleibt
  stehen und der anti_feed-Fallback greift.

## 1.1.0 (2026-09-01)

- **Echter Einstand statt 15-ct-Konstante:** das Add-on fuehrt den gewichteten
  Ist-Einkaufspreis der gespeicherten Energie im State mit (Netzladung zum
  Stundenpreis, PV-Ladung zur Export-Opportunitaet, aus den kumulativen
  Ladezaehlern zwischen zwei Laeufen). Anlass: die 04:00-Ladung zu 32 ct
  all-in wurde ab 04:55 als 15-ct-Ware verplant, Margen waren geschoent.
  `einstand_start` ist nur noch der Startwert bei leerem State.
- **Umschicht-Pass gegen die Greedy-Luecke:** nach dem Greedy-Lauf prueft ein
  Verbesserungs-Pass Dreiecksgeschaefte (frueh teuer verkaufen, spaeter billig
  nachkaufen, Ziel-Stunde behalten; lohnt ab wert(k1) > kost(s)/rt). Jede
  Umschichtung muss einen unabhaengigen Voll-Simulator (alle Kappen, SoC-Bahn,
  Richtungs- und Export-Regel) bestehen UND die Bilanz strikt verbessern,
  sonst bleibt das Greedy-Ergebnis unveraendert. Anlass 2026-09-01: der
  Morgen-Peak um 7 Uhr blieb ungenutzt, obwohl mittags fuer 4 ct Beurs
  nachkaufbar war (~0,10-0,15 EUR verpasst).

## 1.0.1 (2026-08-30)

- Fix Invarianten-Notbremse: Untergrenze der SoC-Bahn ist jetzt min(Boden,
  Ist-Start). Ein real unter dem 12,3-Prozent-Boden stehender Akku (z.B. 11,9 %
  nach einem Entladeabend) blockierte sonst JEDEN Publish; in der Nacht
  29./30.08. fielen so 13 Laeufe in Folge aus und v2 lief bis 12:01 planlos.
- Fehler-Meldungen tragen eine feste notification_id und ersetzen sich damit
  selbst, statt sich stuendlich zu stapeln.

## 1.0.0 (2026-08-29)

- Erste Fassung: Python-Port des Plan-Kerns aus `batterie_schatten.ps1`
  (verlustfreier Greedy-Optimierer, NextEnergy-Wertmodell, Sperrpreis-Logik),
  stuendlicher Replan mit eingefrorener laufender Stunde, Folgetagsplan um
  23:takt, Flatter-Bremse, Invarianten-Notbremse, Status-Sensor per MQTT
  Discovery, Hauslastprofil aus der Recorder-Historie.
