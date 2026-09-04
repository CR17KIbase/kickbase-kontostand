#!/usr/bin/env python3
"""
DIAGNOSE-SKRIPT: Woher kommen die Spieltags-Praemien?
=====================================================

Zweck: Seit Saisonstart gibt es Preisgelder fuer Spieltagspunkte. Die sind fuer
jeden Manager UNTERSCHIEDLICH hoch - damit stimmt die bisherige Annahme nicht
mehr, dass der Bonus fuer alle gleich ist.

Dieses Skript rechnet nichts aus und aendert nichts. Es schaut nur nach, welche
Daten die API dazu liefert, damit wir danach die richtige Formel bauen koennen
(statt sie zu raten). Konkret prueft es drei Quellen:

  1. Activity-Feed  -> enthaelt womoeglich die ECHTEN Bonusbetraege je Manager
                       (Beispiel aus der API-Doku: {"bn": 10000, "day": 1}).
                       Waere der Idealfall: dann brauchen wir gar keine Formel.
  2. Performance    -> Punkte pro Spieltag je Manager. Damit liesse sich eine
                       Praemie pro Punkt herleiten, falls 1. nichts hergibt.
  3. Deine Bilanz   -> zeigt, wie gross der unerklaerte Betrag aktuell ist.

DATENSCHUTZ: Das Actions-Protokoll ist in einem oeffentlichen Repository fuer
jeden lesbar. Deshalb werden Namen und IDs anderer Manager ANONYMISIERT
(Manager A, B, C ...). Passwoerter und Token werden nie ausgegeben.
"""

import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any

import requests

BASE_URL = "https://api.kickbase.com"
LEAGUE_ID = "5567806"
STARTBUDGET = 50_000_000
SEASON_START = "2026-08-03T00:00:00Z"

_alias: dict[str, str] = {}


def anon(user_id: Any) -> str:
    """Vergibt stabile Pseudonyme, damit im oeffentlichen Log keine Klarnamen stehen."""
    key = str(user_id)
    if key not in _alias:
        _alias[key] = (f"Manager {chr(65 + len(_alias))}" if len(_alias) < 26
                       else f"Manager #{len(_alias)}")
    return _alias[key]


def scrub(obj: Any) -> Any:
    """Ersetzt bekannte Personenfelder rekursiv durch Pseudonyme."""
    person_felder = {"u", "unm", "byr", "slr", "buid", "slid", "usr", "uid"}
    entfernen = {"uim", "lim", "plpim"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in entfernen:
                continue
            out[k] = anon(v) if k in person_felder and not isinstance(v, (dict, list)) else scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def main() -> None:
    email = os.environ.get("KICKBASE_EMAIL")
    password = os.environ.get("KICKBASE_PASSWORD")
    if not email or not password:
        print("KICKBASE_EMAIL / KICKBASE_PASSWORD fehlen.")
        sys.exit(1)

    r = requests.post(
        f"{BASE_URL}/v4/user/login",
        json={"em": email, "pass": password, "loy": False, "rep": {}},
        headers={"Accept": "application/json"}, timeout=15,
    )
    print(f"[LOGIN] Status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:400])
        sys.exit(1)

    login = r.json()
    token = login["tkn"]
    me = login["u"]["id"]
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    _alias[str(me)] = "DU"

    for lg in login.get("srvl", []):
        if str(lg.get("id")) == LEAGUE_ID:
            print(f"Liga: {lg.get('name')} ({lg.get('id')})")
            break
    else:
        print(f"!! Liga-ID {LEAGUE_ID} nicht gefunden. Verfuegbar: "
              f"{[(l.get('name'), l.get('id')) for l in login.get('srvl', [])]}")
        sys.exit(1)
    league_id = LEAGUE_ID

    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("[1] ACTIVITY-FEED - stehen hier echte Bonusbetraege je Manager?")
    print("=" * 70)
    feed: list[dict[str, Any]] = []
    start = 0
    seitengroessen: list[int] = []
    abbruchgrund = "Sicherheitslimit erreicht"
    for _ in range(400):  # Sicherheitslimit: bis zu 400 Seiten
        rf = requests.get(f"{BASE_URL}/v4/leagues/{league_id}/activitiesFeed",
                          params={"start": start, "max": 500}, headers=h, timeout=20)
        if rf.status_code != 200:
            abbruchgrund = f"HTTP-Status {rf.status_code}: {rf.text[:150]}"
            break
        chunk = rf.json().get("af", [])
        if not chunk:
            abbruchgrund = "Feed vollstaendig geladen (keine weiteren Eintraege)"
            break
        feed.extend(chunk)
        seitengroessen.append(len(chunk))
        start += len(chunk)

        # Der Feed ist chronologisch (neueste zuerst). Sobald Eintraege vor dem
        # Saisonstart auftauchen, haben wir alles Relevante - alles Aeltere
        # gehoert zur Vorsaison und wird nicht gebraucht.
        time.sleep(0.3)  # kleine Pause, um die API nicht zu ueberlasten

        aeltester = min((e.get("dt") or "9999") for e in chunk)
        if aeltester < SEASON_START:
            abbruchgrund = f"Saisonstart erreicht (aeltester Eintrag {aeltester[:10]})"
            break

    print(f"Eintraege geladen: {len(feed)}")
    print(f"Abbruch: {abbruchgrund}")
    if seitengroessen:
        print(f"Seitengroessen (angefragt: 500): erste={seitengroessen[0]}, "
              f"groesste={max(seitengroessen)} -> zeigt das tatsaechliche API-Limit")
    if feed:
        daten = sorted((e.get("dt") or "") for e in feed if e.get("dt"))
        if daten:
            print(f"Abgedeckter Zeitraum: {daten[0][:16]} bis {daten[-1][:16]}")
            if daten[0] > SEASON_START:
                print(f"  [!] ACHTUNG: reicht NICHT bis zum Saisonstart "
                      f"({SEASON_START[:10]}) zurueck - eine Luecke unten waere damit erklaert.")
            else:
                print(f"  [OK] Reicht bis vor den Saisonstart ({SEASON_START[:10]}) zurueck -")
                print("       es fehlt also nichts aus dieser Saison.")

    if feed:
        typen = Counter(e.get("t") for e in feed)
        print(f"Aktivitaets-Typen (t) und Haeufigkeit: {dict(typen)}")

        print("\nJe Typ ein Beispiel (anonymisiert), damit wir die Struktur sehen:")
        gesehen: set[Any] = set()
        for e in feed:
            t = e.get("t")
            if t in gesehen:
                continue
            gesehen.add(t)
            print(f"  t={t}: {json.dumps(scrub(e), ensure_ascii=False)[:400]}")

        print("\nGeldbetraege in den Feed-Daten - je Aktivitaets-Typ:")
        print("(sucht NICHT nur 'bn', sondern jedes Zahlenfeld, das ein Betrag sein koennte -")
        print(" so fallen auch Transfer- oder Erfolgspraemien auf, die anders heissen)")
        geld_felder: dict[Any, Counter] = defaultdict(Counter)
        for e in feed:
            d = e.get("data")
            if not isinstance(d, dict):
                continue
            for k, v in d.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= 1000:
                    geld_felder[e.get("t")][k] += 1
        if geld_felder:
            for t, felder in sorted(geld_felder.items(), key=lambda x: str(x[0])):
                print(f"  t={t}: Felder mit Betraegen >= 1.000 -> {dict(felder)}")
        else:
            print("  keine gefunden")

        print("\nEintraege mit einem Bonus-Feld ('bn'):")
        bonus = [e for e in feed if isinstance(e.get("data"), dict) and "bn" in e["data"]]
        print(f"  Anzahl: {len(bonus)}")
        if bonus:
            for e in bonus[:8]:
                print(f"    {json.dumps(scrub(e), ensure_ascii=False)[:300]}")
            summe: dict[str, int] = defaultdict(int)
            for e in bonus:
                d = e["data"]
                uid = e.get("u") or d.get("u") or "unbekannt"
                summe[anon(uid)] += d.get("bn", 0) or 0
            print("\n  Summe der gefundenen Boni je Manager:")
            for k, v in sorted(summe.items(), key=lambda x: -x[1]):
                print(f"    {k:<12} {v:>14,.0f} EUR".replace(",", "."))
            print("  >>> Stehen hier je Manager plausible Betraege, brauchen wir KEINE")
            print("      Formel - dann summieren wir einfach diese echten Boni.")
        else:
            print("  -> Keine 'bn'-Felder gefunden. Dann Variante [2] nutzen.")

    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("[2] PERFORMANCE - Punkte pro Spieltag je Manager")
    print("=" * 70)
    rr = requests.get(f"{BASE_URL}/v4/leagues/{league_id}/ranking", headers=h, timeout=15)
    managers = rr.json().get("us", []) if rr.status_code == 200 else []
    print(f"Manager in der Liga: {len(managers)}")

    for m in managers[:4]:  # nur einige, um die API nicht zu belasten
        mid = m.get("i")
        rp = requests.get(f"{BASE_URL}/v4/leagues/{league_id}/managers/{mid}/performance",
                          headers=h, timeout=15)
        if rp.status_code != 200:
            print(f"  {anon(mid)}: Status {rp.status_code}")
            continue
        seasons = rp.json().get("it", [])
        if not seasons:
            print(f"  {anon(mid)}: keine Saisondaten")
            continue
        akt = seasons[-1]
        tage = [d for d in akt.get("it", []) if d.get("mdp") is not None]
        print(f"  {anon(mid):<12} Saison {akt.get('sn')}: {len(tage)} Spieltage, "
              f"{sum(d.get('mdp', 0) for d in tage)} Punkte, "
              f"{sum(1 for d in tage if d.get('tw'))}x tw=true, Platz {akt.get('pl')}")
        if tage:
            print(f"      je Spieltag: {[(d.get('day'), d.get('mdp')) for d in tage]}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("[3] DEINE BILANZ - wie gross ist der unerklaerte Betrag?")
    print("=" * 70)
    rb = requests.get(f"{BASE_URL}/v4/leagues/{league_id}/me/budget", headers=h, timeout=15)
    budget = None
    if rb.status_code == 200:
        d = rb.json()
        if isinstance(d, (int, float)):
            budget = d
        elif isinstance(d, dict):
            budget = next((d[k] for k in ("b", "budget", "bg") if k in d), None)
            if budget is None:
                print(f"  Unerwartete Struktur von /me/budget: {d}")
    print(f"Dein echter Kontostand: {budget}")

    trans: list[dict[str, Any]] = []
    start = 0
    for _ in range(50):
        rt = requests.get(f"{BASE_URL}/v4/leagues/{league_id}/managers/{me}/transfer",
                          params={"start": start}, headers=h, timeout=15)
        if rt.status_code != 200:
            break
        items = rt.json().get("it", [])
        if not items:
            break
        saison = [t for t in items if (t.get("dt") or "") >= SEASON_START]
        trans.extend(saison)
        if len(saison) < len(items):
            break
        start += len(items)

    kauf = sum(t.get("trp", 0) for t in trans if t.get("tty") == 1)
    verkauf = sum(t.get("trp", 0) for t in trans if t.get("tty") == 2)
    print(f"Transfers seit {SEASON_START[:10]}: {len(trans)} Stueck")
    print(f"  Kaeufe:    {kauf:>15,.0f} EUR".replace(",", "."))
    print(f"  Verkaeufe: {verkauf:>15,.0f} EUR".replace(",", "."))
    if budget is not None:
        rest = budget - STARTBUDGET + kauf - verkauf
        print("\n  Unerklaerter Rest (= SAEMTLICHE Praemien: Login, Spieltag,")
        print("  Transfer, Erfolge ... - eine Auffangrechnung, da faellt nichts raus):")
        print(f"    {rest:,.0f} EUR".replace(",", "."))

        # --- Vollstaendigkeitspruefung -----------------------------------
        print("\n" + "-" * 70)
        print("  VOLLSTAENDIGKEITSPRUEFUNG")
        print("-" * 70)
        meine_boni = 0
        treffer = 0
        for e in feed:
            d = e.get("data")
            if not isinstance(d, dict):
                continue
            uid = str(e.get("u") or d.get("u") or "")
            if uid != str(me):
                continue
            for k, v in d.items():
                if k in ("bn", "bonus") and isinstance(v, (int, float)) and not isinstance(v, bool):
                    meine_boni += v
                    treffer += 1
        print(f"  Im Feed gefundene Boni fuer DICH: {meine_boni:,.0f} EUR "
              f"aus {treffer} Eintraegen".replace(",", "."))
        print(f"  Tatsaechlicher Gesamtbetrag:      {rest:,.0f} EUR".replace(",", "."))
        luecke = rest - meine_boni
        print(f"  Differenz:                        {luecke:,.0f} EUR".replace(",", "."))
        if treffer == 0:
            print("  ==> Der Feed liefert fuer dich keine Bonus-Eintraege. Dann scheidet")
            print("      Weg A aus und wir gehen ueber die Punkte (Weg B).")
        elif luecke == 0:
            print("  ==> PERFEKT: Die gefundenen Boni erklaeren den Betrag exakt.")
            print("      Damit ist bewiesen, dass ALLE Praemienarten erfasst sind.")
        else:
            print("  ==> Es fehlt noch etwas. Die Differenz zeigt, wie viel an Praemien")
            print("      im Feed nicht auftaucht (evtl. anderer Feldname, oder der Feed")
            print("      reicht zeitlich nicht weit genug zurueck).")
        print("-" * 70)

    print("\n" + "=" * 70)
    print("FERTIG - bitte diese Ausgabe zurueckschicken.")
    print("=" * 70)


if __name__ == "__main__":
    main()
