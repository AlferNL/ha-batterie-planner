# Changelog

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
