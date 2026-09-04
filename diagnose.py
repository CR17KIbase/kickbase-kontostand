#!/usr/bin/env python3
"""
DIAGNOSE 3: Ist der Kontostand = Rangliste-tv minus Dashboard-tv?
==================================================================

Beobachtung aus Diagnose 2: Fuer denselben Manager liefern zwei Endpunkte
unterschiedliche "tv"-Werte:
    Rangliste: 215.794.865
    Dashboard: 205.852.562
    Differenz:   9.942.303   <- sieht nach einem Kontostand aus

VERMUTUNG: Die Rangliste zeigt das Gesamtvermoegen (Teamwert + Kontostand),
das Dashboard nur den reinen Teamwert. Dann waere:

    Kontostand = Rangliste-tv - Dashboard-tv

Waere das richtig, braeuchten wir KEINE Berechnung mehr: keine Transfers, keine
Boni, keine Spieltagspraemien, keine Annahmen. Wir wuerden die echten
Kontostaende einfach auslesen.

BEWEIS: Deinen eigenen Kontostand kennen wir exakt (/me/budget). Wenn die Formel
bei DIR auf den Euro genau stimmt, gilt sie auch fuer die anderen.

DATENSCHUTZ: Namen und IDs werden pseudonymisiert (das Log ist oeffentlich).
"""

import os
import sys
import time
from typing import Any

import requests

BASE_URL = "https://api.kickbase.com"
LEAGUE_ID = "5567806"

_alias: dict[str, str] = {}


def anon(v: Any) -> str:
    k = str(v)
    if k not in _alias:
        _alias[k] = (f"Manager {chr(65 + len(_alias))}" if len(_alias) < 26
                     else f"Manager #{len(_alias)}")
    return _alias[k]


def eur(x: float | None) -> str:
    return "?" if x is None else f"{x:,.0f}".replace(",", ".")


def main() -> None:
    email, password = os.environ.get("KICKBASE_EMAIL"), os.environ.get("KICKBASE_PASSWORD")
    if not email or not password:
        print("KICKBASE_EMAIL / KICKBASE_PASSWORD fehlen."); sys.exit(1)

    r = requests.post(f"{BASE_URL}/v4/user/login",
                      json={"em": email, "pass": password, "loy": False, "rep": {}},
                      headers={"Accept": "application/json"}, timeout=15)
    if r.status_code != 200:
        print(f"[LOGIN] Status {r.status_code}: {r.text[:300]}"); sys.exit(1)
    login = r.json()
    h = {"Authorization": f"Bearer {login['tkn']}", "Accept": "application/json"}
    me = str(login["u"]["id"])
    _alias[me] = "DU"
    lid = LEAGUE_ID

    # Reihenfolge wichtig: erst Rangliste, dann Budget, dann Dashboards -
    # die Werte sollen moeglichst aus demselben Moment stammen.
    rr = requests.get(f"{BASE_URL}/v4/leagues/{lid}/ranking", headers=h, timeout=20)
    ranking = rr.json().get("us", []) if rr.status_code == 200 else []

    rb = requests.get(f"{BASE_URL}/v4/leagues/{lid}/me/budget", headers=h, timeout=15)
    d = rb.json() if rb.status_code == 200 else {}
    mein_budget = d if isinstance(d, (int, float)) else next(
        (d[k] for k in ("b", "budget", "bg") if isinstance(d, dict) and k in d), None)
    print(f"Dein echter Kontostand (/me/budget): {eur(mein_budget)} EUR\n")

    print("=" * 88)
    print(f"{'Manager':<12}{'Rangliste tv':>18}{'Dashboard tv':>18}{'Differenz':>16}{'prft':>18}")
    print("=" * 88)

    zeilen = []
    for m in ranking:
        mid = str(m.get("i"))
        rank_tv = m.get("tv")
        rd = requests.get(f"{BASE_URL}/v4/leagues/{lid}/managers/{mid}/dashboard",
                          headers=h, timeout=15)
        time.sleep(0.3)
        if rd.status_code != 200:
            print(f"{anon(mid):<12} Dashboard-Status {rd.status_code}")
            continue
        dash = rd.json()
        dash_tv = dash.get("tv")
        diff = (rank_tv - dash_tv) if (rank_tv is not None and dash_tv is not None) else None
        zeilen.append((mid, rank_tv, dash_tv, diff))
        print(f"{anon(mid):<12}{eur(rank_tv):>18}{eur(dash_tv):>18}{eur(diff):>16}"
              f"{eur(dash.get('prft')):>18}")

    print("=" * 88)

    # ---- Der eigentliche Beweis ----------------------------------------
    print("\n" + "-" * 70)
    print("  BEWEIS an deinen eigenen Daten")
    print("-" * 70)
    meine = next((z for z in zeilen if z[0] == me), None)
    if meine is None or mein_budget is None:
        print("  Konnte deine Zeile nicht bilden - Beweis nicht moeglich.")
    else:
        _, rank_tv, dash_tv, diff = meine
        print(f"  Rangliste tv:            {eur(rank_tv):>18}")
        print(f"  Dashboard tv:            {eur(dash_tv):>18}")
        print(f"  Differenz:               {eur(diff):>18}")
        print(f"  Dein echter Kontostand:  {eur(mein_budget):>18}")
        if diff is None:
            print("  ==> Werte fehlen.")
        elif abs(diff - mein_budget) < 1:
            print("\n  ==> BESTAETIGT: Die Differenz IST der Kontostand (exakt).")
            print("      Damit koennen wir alle Kontostaende direkt auslesen -")
            print("      ohne Transfers, Boni oder Praemien zu berechnen.")
        elif abs(diff - mein_budget) < 200_000:
            print(f"\n  ==> FAST: Abweichung {eur(abs(diff - mein_budget))} EUR.")
            print("      Vermutlich stammen die beiden Werte aus leicht")
            print("      unterschiedlichen Momenten (Marktwert-Update).")
        else:
            print(f"\n  ==> WIDERLEGT: Abweichung {eur(abs(diff - mein_budget))} EUR.")
            print("      Die Differenz bedeutet etwas anderes.")
            if dash_tv is not None:
                print(f"      Zur Info: Dashboard-tv + dein Kontostand = "
                      f"{eur(dash_tv + mein_budget)}")
                print(f"                Rangliste-tv                  = {eur(rank_tv)}")
    print("-" * 70)


if __name__ == "__main__":
    main()
