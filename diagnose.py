#!/usr/bin/env python3
"""
DIAGNOSE 2: Liefert die API die Kontostaende der anderen vielleicht direkt?
===========================================================================

Ausgangslage nach Diagnose 1:
  - Der Activity-Feed reicht nur ~2,5 Wochen zurueck, nicht bis zum Saisonstart.
  - Der Tagesbonus (aktuell 100.000 EUR) steht dort ohne Nutzer-ID, gilt also
    offenbar ligaweit gleich.
  - Spieltagspraemien tauchen im Feed NICHT mit Betraegen auf.

Bevor wir eine Praemienformel rekonstruieren (fehleranfaellig), pruefen wir das
Naheliegende: Vielleicht steht der Kontostand anderer Manager laengst irgendwo
in der API.

DER TRICK: Deinen EIGENEN Kontostand kennen wir exakt (aus /me/budget). Dieses
Skript ruft ihn ab und durchsucht dann alle in Frage kommenden Endpunkte
rekursiv nach genau diesem Zahlenwert. Taucht er irgendwo auf, wissen wir
zweifelsfrei, welches Feld der Kontostand ist - und koennen ihn danach fuer
ALLE Manager direkt auslesen, ohne jede Schaetzung.

Zusaetzlich werden die Rohstrukturen ausgegeben, damit wir sehen, welche
weiteren Kennzahlen (z.B. Teamwert) verfuegbar sind.

DATENSCHUTZ: Das Actions-Protokoll ist oeffentlich lesbar. Namen und IDs
anderer Manager werden pseudonymisiert (Manager A, B, C ...).
"""

import json
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


def scrub(obj: Any) -> Any:
    """Pseudonymisiert Personenfelder und entfernt Bild-URLs (rein visuelles Rauschen)."""
    personen = {"u", "unm", "byr", "slr", "buid", "slid", "usr", "uid", "n", "ln", "fn", "pn"}
    weg = {"uim", "lim", "plpim", "pim", "tim", "im"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in weg:
                continue
            if k in personen and not isinstance(v, (dict, list)):
                out[k] = anon(v)
            else:
                out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def suche_wert(obj: Any, ziel: float, pfad: str = "") -> list[str]:
    """Durchsucht eine JSON-Struktur rekursiv nach einem Zahlenwert und gibt die Fundpfade zurueck."""
    treffer: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            treffer += suche_wert(v, ziel, f"{pfad}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            treffer += suche_wert(v, ziel, f"{pfad}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if abs(float(obj) - ziel) < 0.5:
            treffer.append(pfad or "<wurzel>")
    return treffer


def hole(url: str, headers: dict, params: dict | None = None) -> tuple[int, Any]:
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=20)
        time.sleep(0.3)
        if r.status_code != 200:
            return r.status_code, None
        return 200, r.json()
    except Exception as exc:  # Netzwerk-/JSON-Fehler sollen die Diagnose nicht abbrechen
        print(f"    [Fehler] {type(exc).__name__}: {exc}")
        return -1, None


def main() -> None:
    email = os.environ.get("KICKBASE_EMAIL")
    password = os.environ.get("KICKBASE_PASSWORD")
    if not email or not password:
        print("KICKBASE_EMAIL / KICKBASE_PASSWORD fehlen.")
        sys.exit(1)

    r = requests.post(f"{BASE_URL}/v4/user/login",
                      json={"em": email, "pass": password, "loy": False, "rep": {}},
                      headers={"Accept": "application/json"}, timeout=15)
    print(f"[LOGIN] Status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300]); sys.exit(1)

    login = r.json()
    token = login["tkn"]
    me = str(login["u"]["id"])
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    _alias[me] = "DU"
    lid = LEAGUE_ID

    # --- Mein exakter Kontostand als Suchmuster --------------------------
    st, bud = hole(f"{BASE_URL}/v4/leagues/{lid}/me/budget", h)
    mein_budget = None
    if st == 200:
        mein_budget = bud if isinstance(bud, (int, float)) else next(
            (bud[k] for k in ("b", "budget", "bg") if isinstance(bud, dict) and k in bud), None)
    print(f"Dein Kontostand (Suchmuster): {mein_budget}")
    if mein_budget is None:
        print("!! Ohne bekannten Kontostand ist die Suche sinnlos.")
        sys.exit(1)

    # --- Ranking: welche Manager gibt es? --------------------------------
    st, ranking = hole(f"{BASE_URL}/v4/leagues/{lid}/ranking", h)
    managers = (ranking or {}).get("us", [])
    andere = [str(m.get("i")) for m in managers if str(m.get("i")) != me]
    print(f"Manager in der Liga: {len(managers)} (davon andere: {len(andere)})")

    # --- Kandidaten-Endpunkte durchsuchen --------------------------------
    ein_anderer = andere[0] if andere else me
    kandidaten = [
        ("ranking",                f"{BASE_URL}/v4/leagues/{lid}/ranking", None),
        ("overview",               f"{BASE_URL}/v4/leagues/{lid}/overview",
                                   {"includeManagersAndBattles": "true"}),
        ("me",                     f"{BASE_URL}/v4/leagues/{lid}/me", None),
        ("settings/managers",      f"{BASE_URL}/v4/leagues/{lid}/settings/managers", None),
        ("dashboard (DU)",         f"{BASE_URL}/v4/leagues/{lid}/managers/{me}/dashboard", None),
        ("dashboard (anderer)",    f"{BASE_URL}/v4/leagues/{lid}/managers/{ein_anderer}/dashboard", None),
        ("teamcenter (anderer)",   f"{BASE_URL}/v4/leagues/{lid}/users/{ein_anderer}/teamcenter", None),
        ("squad (anderer)",        f"{BASE_URL}/v4/leagues/{lid}/managers/{ein_anderer}/squad", None),
    ]

    print("\n" + "=" * 70)
    print(f"[1] SUCHE nach dem Wert {mein_budget:,.0f} in allen Endpunkten".replace(",", "."))
    print("=" * 70)
    fundstellen: list[tuple[str, str]] = []
    rohdaten: dict[str, Any] = {}
    for name, url, params in kandidaten:
        st, data = hole(url, h, params)
        if st != 200 or data is None:
            print(f"  {name:<22} Status {st} - uebersprungen")
            continue
        rohdaten[name] = data
        tref = suche_wert(data, float(mein_budget))
        if tref:
            print(f"  {name:<22} TREFFER an: {tref}")
            fundstellen += [(name, t) for t in tref]
        else:
            print(f"  {name:<22} kein Treffer")

    print("\n" + "-" * 70)
    if fundstellen:
        print("  ==> GEFUNDEN! Der Kontostand steht in der API. Relevante Stellen:")
        for name, pfad in fundstellen:
            print(f"      {name}: {pfad}")
        print("  Wenn eine dieser Stellen auch fuer ANDERE Manager gefuellt ist,")
        print("  brauchen wir keine Schaetzung mehr - dann lesen wir sie direkt aus.")
    else:
        print("  ==> Nirgends gefunden. Der Kontostand anderer Manager ist also")
        print("      nicht direkt abrufbar; wir muessen ihn weiter berechnen.")
    print("-" * 70)

    # --- Rohstrukturen zeigen -------------------------------------------
    print("\n" + "=" * 70)
    print("[2] ROHSTRUKTUREN (gekuerzt) - welche Kennzahlen gibt es ueberhaupt?")
    print("=" * 70)
    for name in ("ranking", "overview", "dashboard (DU)", "dashboard (anderer)",
                 "teamcenter (anderer)"):
        if name not in rohdaten:
            continue
        print(f"\n--- {name} ---")
        print(json.dumps(scrub(rohdaten[name]), ensure_ascii=False, indent=1)[:1800])

    # --- Ranking je Manager im Klartext ----------------------------------
    if managers:
        print("\n" + "=" * 70)
        print("[3] RANKING-FELDER je Manager (zum Abgleich mit der App)")
        print("=" * 70)
        print(f"  Felder eines Ranking-Eintrags: {sorted(managers[0].keys())}")
        for m in managers:
            zahlen = {k: v for k, v in m.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)}
            print(f"  {anon(m.get('i')):<12} {zahlen}")

    print("\n" + "=" * 70)
    print("FERTIG - bitte diese Ausgabe zurueckschicken.")
    print("=" * 70)


if __name__ == "__main__":
    main()
