# Changelog

## 1.3.5 (2026-09-04)

- **Halte-Schwelle fuer Startinhalt vom Einstand geloest:** der Greedy mass
  gespeicherte Ware bisher am historischen Einstand, obwohl der bezahlt ist
  (Sunk Cost). Unter Saldering fiel das kaum auf, weil der um 11 ct hoehere
  Exportwert fast immer eine Abend-Ankerbuchung lieferte, die der
  Umschicht-Pass (1.3.0) nach vorn zog. Replay 2026-09-04 des 06:48-Laufs vom
  03.09. OHNE Saldering (Einstand 33,4 ct, SoC 60 %): kein einziger
  Startinhalt-Trade, 2,44 kWh lagen den ganzen Tag ungenutzt im Akku,
  waehrend das Haus morgens zu 36 ct aus dem Netz lief (-0,25 EUR statt
  moeglicher +1,07 EUR). Neu: `reservationswert()` = Wert einer gespeicherten
  kWh ueber den Horizont hinaus, gedeckelt durch die beste Stunde des
  Referenztags und den billigsten Nachkauf dort; Referenz ist der Folgetag,
  sobald seine Beurs-Kurve da ist (ab ~13 Uhr), sonst der geplante Tag mit
  voller Kurve. `optimiere()` nimmt die Schwelle als `reservation`; ohne
  Angabe gilt weiter der Einstand (nur noch im Selbsttest als Referenz). Die
  Log-Zeile nennt Schwelle und Quelle (`Halte-Schwelle 18.8 ct aus Folgetag`).
- **Selbsttest um die Welt ohne Saldering erweitert** (Szenario 4 und 4b):
  Morgenstunde 7 muss bedient und der Akku am Tagesende leer sein, die
  Halte-Schwelle muss gegen das alte Einstand-Gate mindestens 5 ct bringen;
  Gegenprobe 4b (kein billiger Nachkauf) darf die Morgenstunde nicht
  verkaufen. Bis 1.3.4 war die AUS-Welt durch keinen Regressionsfall
  abgesichert (der Selbsttest liest den HA-Schalter nie).
- **Regime-Kennzeichen:** Plan-Payload (`saldering`), Status-Attribute und
  `last_plan.json` tragen, in welchem Abrechnungsmodell der Plan entstand.
  Ein Wechsel des Schalters publiziert den Plan auch dann, wenn die Aktionen
  gleich bleiben (Flatter-Bremse prueft das Regime mit).
- **Rueckfall bei unlesbarem Schalter nach Kalender** (`jetzt().year < 2027`)
  statt still `True`, mit WARNUNG im Log.
- Befund zur Einordnung (Replay 2026-09-04, Harness validiert gegen die
  Add-on-Logs): der Schalter wirkt im Planer an genau einer Stelle
  (`wert_terug`, minus 0,1108 EUR/kWh Teruglever-Wert). Ohne Saldering
  stirbt der Morgen-Export aus der Nachtladung, PV-Ueberschuss wird zur
  bevorzugten Ladequelle, der Kipp-Punkt Export gegen Eigenverbrauch wandert
  von 4 auf 27 ct Beurs. In der echten Saldering-Welt kostet ein vorzeitiges
  Umschalten Cent pro Tag, nicht Euro.

## 1.3.4 (2026-09-03)

- **Export aus dem Akku nur im Zonnebonus-Fenster** (`ne_zonnebonus_start`
  bis `ne_zonnebonus_ende`, aktuell 06 bis 22 Uhr). Ausserhalb bekommt eine
  Entladestunde nur noch den Hausbedarf (Nulleinspeisung, Aktion
  `ANTI_FEED`), Greedy und Umschicht-Pass bilden dort keinen Exportblock.
  Anlass 2026-09-03 22 Uhr: 800-W-Block mit 0,08 kWh geplantem Export zu
  33 ct (Beurs + Belasting unter Saldering), real 150 W ins Netz. Unter
  Saldering rechnerisch ein Nullsummenspiel gegen den Hausverbrauch um 23
  Uhr, aber Andres Regel: gespeicherte Ware geht ohne Bonus nicht ins Netz.
  Selbsttest-Szenario 3 sichert die Regel.

## 1.3.3 (2026-09-03)

- **Vergangene Stunden behalten ihre Aktion:** beim Publizieren tragen die
  Stunden vor der Planstunde die Aktion des zuletzt publizierten Plans
  desselben Tages statt `VORBEI`. Befund 2026-09-03 19:31: nach dem
  Add-on-Neustart um 19:15 stand die laufende Stunde 19 als VORBEI im
  Plan-Sensor, der HA-Failsafe (prueft um :31 nur auf ZWANGSLADUNG und
  ZWANGSENTLADUNG) hielt die laufende, geplante Entladung fuer ungeplant und
  raeumte sie ab. Der Executor prueft vor jedem Befehl den Ist-Zustand und
  ist gegen die uebernommenen Aktionen idempotent.

## 1.3.2 (2026-09-03)

- **Fester Einkaufsaufschlag statt Live-Ableitung:** fuer Stunden ohne
  NextEnergy-Stundenpreis (Folgetag) ist der Aufschlag auf den Beurs-Preis
  jetzt Energiebelasting + Inkoopvergoeding aus den HA-Helfern
  (`input_number.ne_energiebelasting` + `input_number.ne_inkoopvergoeding`,
  aktuell 0,1108 + 0,0219 = 0,1327 EUR/kWh). Die bisherige Ableitung aus
  "huidige Prijs minus Beurs der laufenden Stunde" sprang durch die
  Cent-Rundung beider Sensoren zwischen 0,12 und 0,14 und kippte am
  2026-09-03 die Morgenmarge mit. Die Live-Ableitung bleibt als
  Plausibilitaetspruefung (WARNUNG bei mehr als 1,5 ct Abweichung). Fehlt
  der Inkoop-Helfer, gilt die alte Live-Ableitung.
- Restrauschen von +/-1 ct bleibt, weil EnergyZero den Beurs-Preis auf ganze
  Cent rundet; fuer heutige Stunden nutzt der Planer seit 1.3.1 die
  NextEnergy-Stundenpreise direkt.

## 1.3.1 (2026-09-03)

- **Hausprofil aus den Geraetezaehlern der Marstek statt aus den
  HA-Integrationshelfern:** `tages_profil` rechnet Lade- und Entladeenergie
  je Stunde jetzt aus `sensor.marstek_venus_modbus_gesamte_ladeenergie` und
  `..._gesamte_entladeenergie` (Modbus, ticken alle ~60 s in
  0,01-kWh-Schritten). Die bisherigen Helfer `batterij_ontladen_kwh` und
  `batterij_laden_uit_net/pv_kwh` sind Riemann-Integratoren ueber eine
  Leistung, die bei konstanter Ladung stundenlang nicht tickt; sie buchten
  eine 3,77-kWh-Nachtladung komplett in Stunde 5 und 2,5 kWh Abendentladung
  in Stunde 22. Im Profil wurde daraus 1,5 kWh Hauslast um 3 und 4 Uhr, 1,3
  kWh "PV-Ueberschuss" um 5 Uhr und 3 kWh Hauslast um 22 Uhr. Mit dem seit
  1.3.0 wirksamen 3-Tage-Median haette der Folgetagsplan fuer den 04.09.
  darauf gebaut (ANTI_FEED um 5 Uhr und 7 Uhr, um Phantom-PV einzulagern).
- Gecachte Tagesprofile im State werden einmalig verworfen und aus der
  Recorder-Historie neu gerechnet (`profil_version` 2).
- **Einstand lag-frei und stundengenau:** der Zugang seit dem letzten Lauf
  wird aus den Live-Leistungssensoren `batterij_laden_uit_net_w` /
  `_pv_w` je Kalenderstunde integriert und mit dem Preis SEINER Stunde
  bewertet. Vorher lieferten die kWh-Helfer den Zugang um Stunden verspaetet
  in einem Schub, der mit dem Preis der Fenster-Mitte bewertet wurde,
  waehrend der Snapshot-SoC die Ware schon enthielt (doppelte Gewichtung).
  Befund 2026-09-03: Einstand nach der Nachtladung erst zu niedrig (31,6 ct,
  Stunde-3-Ware als Altbestand zum Vortagspreis), dann Sprung auf 33,4 ct
  um 06:48 mit dem Preis der Stunde 6 fuer Ware aus den Stunden 4 und 5.
  Ein zu langes Fenster (> 3 h, z.B. nach Ausfall) wird jetzt als WARNUNG
  gemeldet statt still verworfen; bei HA-API-Fehler bleibt das Fenster offen.
- **Einkaufspreis je Stunde aus NextEnergy** (`hourly_prices` des Sensors
  `next_energy_huidige_prijs`), solange die Stunde dort vorliegt (heute);
  sonst wie bisher Beurs + Aufschlag der laufenden Stunde (Folgetag). Der
  eine Aufschlag rundete je Stunde um +/-1 ct daneben und kippte am
  2026-09-03 die 07h-Marge mit (0,14 um 05:48, 0,13 um 06:48).
- Selbsttest laeuft still (keine Phantom-UMSCHICHTUNG-Zeilen im Log beim
  Start) und nennt die drei Szenarien statt einer alten Versionsnummer.

## 1.3.0 (2026-09-03)

- **Sunk-Cost-Gate im Umschicht-Pass gestrichen:** fuer bereits gespeicherte
  Ware (Startinhalt) verlangte der Pass bisher, dass der fruehe Verkauf ueber
  dem historischen Einstand liegt, bevor er das Dreieck (frueh verkaufen,
  spaeter billig nachkaufen) ueberhaupt bewertete. Der Einstand ist aber
  bezahlt; der einzige Massstab ist der Nachkaufpreis, den die
  Kern-Ungleichung (wert(k1) > kost(s)/rt + puffer) samt Voll-Simulator und
  Strikt-Besser-Regel weiterhin prueft. Fuer noch nicht gekaufte Ware
  (netz/pv-Quelle) bleibt das Gate. Befund 2026-09-03: nach der Nachtladung
  sprang der Einstand um 06:48 auf 33,4 ct (Ladezaehler buchte zwei Stunden
  in einem Schub nach), die Morgenbloecke 07h/08h zu 36 ct fielen unter die
  1-ct-Schwelle, das Gate blockte den Verkauf mit Nachkauf um 14 Uhr zu
  17 ct (+16 ct/kWh), der Akku stand ab 07:01 den Tag ueber mit ~1 kWh
  ungenutzt in Ruhe. Replay des 06:48-Laufs mit Fix: +0,22 EUR. Hinweis
  zum Log: die Marge in einer `Startinhalt ... -> haus 07h (x ct)`-Zeile
  bleibt relativ zum Einstand gerechnet und kann nach einer Umschichtung
  negativ stehen; der echte Gewinn des Zuges steht in der
  `UMSCHICHTUNG`-Zeile (Verkauf minus Nachkauf).
- **`profil_tage` wirkt jetzt wie beschriftet:** der Median laeuft nur noch
  ueber die juengsten `profil_tage` Tage mit Daten. Bisher behielt der State
  `profil_tage + 6` Tage als Vorrat und der Median lief ueber alle, die
  Einstellung 3 war faktisch ein 9-Tage-Median.
- **Selbsttest um das Sunk-Cost-Szenario erweitert** (06:48-Lauf vom
  2026-09-03): der Umschicht-Pass muss die Morgenstunde trotz Einstand ueber
  dem Morgenpreis bedienen, und kein Verkauf darf unter den Nachkaufkosten
  liegen.

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
