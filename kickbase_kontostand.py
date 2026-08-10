#!/usr/bin/env python3
"""
Kickbase Liga-Kontostand-Tool
=============================

Zeigt fuer alle Teilnehmer deiner Kickbase-Liga den GESCHAETZTEN Kontostand an.

WICHTIG - bitte vorher lesen:
------------------------------
Kickbase bietet keine oeffentliche, offizielle API. Dieses Skript nutzt die von
der Community reverse-engineerte "API v4" (siehe github.com/kevinskyba/kickbase-api-doc).
Das ist explizit "unofficial" und rein zu privaten/Lern-Zwecken gedacht - Kickbase
koennte Endpunkte aendern oder exzessive Nutzung blocken. Bitte das Skript nicht
in Dauerschleife laufen lassen, sondern nur bei Bedarf (z.B. 1x pro Woche).

Ich konnte dieses Skript NICHT gegen die echte Kickbase-API testen (mein Sandbox-
Netzwerk kann api.kickbase.com nicht erreichen), es basiert auf der dokumentierten
Response-Struktur. Beim ersten Lauf lohnt sich ein kritischer Blick auf die Ausgabe.

WARUM "GESCHAETZT"?
-------------------
Kickbase zeigt in der App nur den EIGENEN Kontostand direkt an (Endpoint
/me/budget). Der Kontostand anderer Manager ist nicht oeffentlich sichtbar -
genau deshalb hast du das bisher ueber Screenshots der Transfer-Historie
rekonstruiert. Dieses Skript automatisiert exakt das:

    geschaetzter Kontostand = Startbudget + aktueller Gesamt-Bonus (dynamisch!)
                              + Summe aller Verkaeufe
                              - Summe aller Einkaeufe

WICHTIG: der Bonus (z.B. taeglicher Login-Bonus) ist NICHT als fixer Wert im
Code hinterlegt, weil er sich aendern kann (z.B. 80k/Tag, dann 90k/Tag, dann
100k/Tag - oder irgendein anderes Schema). Stattdessen MISST das Skript den
Bonus bei jedem Lauf live:

  1. Es vergleicht deinen echten Kontostand (/me/budget) mit dem beim letzten
     Lauf gespeicherten Wert.
  2. Es zieht davon ab, was sich durch DEINE eigenen (bekannten) Transfers seit
     dem letzten Lauf erklaeren laesst.
  3. Was uebrig bleibt, MUSS der Bonus-Zuwachs seit dem letzten Lauf sein -
     unabhaengig davon, wie hoch er tatsaechlich ist oder wie er sich aendert.
  4. Dieser gemessene Gesamt-Bonus wird dann auf alle anderen Teilnehmer
     uebertragen (Annahme: der Bonus ist fuer alle in der Liga gleich hoch -
     individuelles Login-Verhalten anderer Manager kann von aussen nicht
     geprueft werden).

Dafuer muss das Skript sich Daten zwischen zwei Laeufen merken (gespeichert in
kickbase_state.json). Beim allerersten Lauf (noch kein gespeicherter Zustand)
wird einmalig eine Anfangs-Kalibrierung durchgefuehrt (Details siehe
initial_calibration()).

WICHTIG zur Vorzeichen-Zuordnung (Kauf vs. Verkauf):
Die API sagt nirgendwo, ob Transfer-Typ 1 ein Kauf oder ein Verkauf ist. Das
laesst sich NICHT allein aus deinen eigenen Daten herleiten, weil der Bonus ein
freier, unbekannter Parameter ist - rechnerisch passt JEDE Vorzeichen-Wahl exakt
zu deinem eigenen Kontostand (man waehlt den Bonus einfach passend dazu).
Deshalb nutzt die Kalibrierung die GESAMTE LIGA: von den moeglichen Vorzeichen-
Kombinationen wird diejenige gewaehlt, bei der die geschaetzten Kontostaende
aller Teilnehmer am plausibelsten zusammenliegen (kleinste Streuung) - in einer
jungen Liga mit gleichem Startbudget sollten die Kontostaende nicht um hunderte
Millionen Euro auseinanderliegen.

WICHTIG: SAISON-FILTER
-----------------------
Die Transferhistorie-Endpunkte der Kickbase-API liefern (laut Beispieldaten der
Community-Doku) die KOMPLETTE Historie ueber mehrere Saisons hinweg, nicht nur
die aktuelle Saison. Da eure Liga aus einer fruehreren Saison weiterlaeuft und
der Kontostand zum Saisonstart auf 50 Mio. zurueckgesetzt wurde, wuerden alte
Transfers aus der Vorsaison die Berechnung verfaelschen, wenn man sie mitzaehlt.
Deshalb filtert das Skript alle Transfers vor SEASON_START (siehe CONFIG unten)
konsequent heraus - sowohl bei der Kalibrierung als auch bei der Bonus-Messung.

EINRICHTUNG (lokal)
-------------------
1. pip install requests
2. Zugangsdaten NICHT im Code speichern, sondern als Umgebungsvariablen setzen:

     export KICKBASE_EMAIL="deine-email@example.com"
     export KICKBASE_PASSWORD="dein-passwort"
     export TELEGRAM_BOT_TOKEN="123456:ABC..."   (optional, siehe unten)
     export TELEGRAM_CHAT_ID="123456789"          (optional, siehe unten)

   (Windows PowerShell: $env:KICKBASE_EMAIL="...")

3. Ggf. unten in CONFIG den Liganamen, das Startbudget und SEASON_START anpassen.
4. python3 kickbase_kontostand.py

TELEGRAM (fuer automatischen Versand, z.B. aus GitHub Actions)
----------------------------------------------------------------
Wenn TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID gesetzt sind, schickt das Skript das
Ergebnis zusaetzlich als Nachricht an einen privaten Telegram-Bot - so kannst du es
jederzeit per iPhone-App oder unter web.telegram.org im Browser nachlesen, ohne
irgendeine eigene Webseite hosten zu muessen. Sind die beiden Variablen nicht
gesetzt, gibt das Skript das Ergebnis einfach nur in der Konsole aus.
"""

import hashlib
import itertools
import json
import os
import sys
import time
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# CONFIG - hier anpassen
# ---------------------------------------------------------------------------

LEAGUE_NAME = "TSV Tiefenbach 25/26"   # muss exakt dem Liganamen in Kickbase entsprechen
STARTBUDGET = 50_000_000              # Startbudget lt. Liga-Einstellungen (das aendert sich normalerweise nicht)
SEASON_START = "2026-08-03T00:00:00Z" # Datum des Kontostand-Resets (03.08., Uhrzeit unbekannt ->
                                       # sicherheitshalber Tagesbeginn UTC). Transfers VOR diesem
                                       # Zeitpunkt werden ignoriert (gehoeren zur Vorsaison).

BASE_URL = "https://api.kickbase.com"
REQUEST_PAUSE_SECONDS = 0.4           # kleine Pause zwischen Requests, um die API nicht zu stressen
STATE_FILE = "kickbase_state.json"    # merkt sich Zustand zwischen zwei Laeufen (fuer den dynamischen Bonus)


# ---------------------------------------------------------------------------
# API-Hilfsfunktionen
# ---------------------------------------------------------------------------

def login(email: str, password: str) -> dict[str, Any]:
    resp = requests.post(
        f"{BASE_URL}/v4/user/login",
        json={"em": email, "pass": password, "loy": False, "rep": {}},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Login fehlgeschlagen (Status {resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()


def find_league_id(login_data: dict[str, Any], league_name: str) -> str:
    leagues = login_data.get("srvl", [])
    for lg in leagues:
        if lg.get("name", "").strip().lower() == league_name.strip().lower():
            return lg["id"]
    available = [lg.get("name") for lg in leagues]
    raise ValueError(
        f"Liga '{league_name}' nicht in deinem Account gefunden.\n"
        f"Verfuegbare Ligen: {available}\n"
        f"-> Passe LEAGUE_NAME in den CONFIG-Einstellungen oben im Skript an."
    )


def get_ranking(token: str, league_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"{BASE_URL}/v4/leagues/{league_id}/ranking",
        headers=_auth_headers(token),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_my_budget(token: str, league_id: str) -> Optional[int]:
    resp = requests.get(
        f"{BASE_URL}/v4/leagues/{league_id}/me/budget",
        headers=_auth_headers(token),
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    # Feldname im Doc nicht 100% spezifiziert - wir probieren die plausibelsten Keys.
    for key in ("b", "budget", "bg"):
        if isinstance(data, dict) and key in data:
            return data[key]
    if isinstance(data, (int, float)):
        return int(data)
    print(f"  [Hinweis] Unerwartete Struktur von /me/budget: {data}")
    return None


def get_manager_transfers(token: str, league_id: str, manager_id: str) -> list[dict[str, Any]]:
    """
    Holt die komplette (paginierte) Transferhistorie eines Managers und filtert
    dabei alles vor SEASON_START heraus (Transfers aus einer Vorsaison).

    Die Eintraege kommen absteigend chronologisch (neueste zuerst) - sobald wir
    also den ersten Eintrag vor SEASON_START sehen, koennen wir das Blaettern
    komplett abbrechen, weil alles Weitere noch aelter waere. Das spart auch
    unnoetige Anfragen an die Kickbase-API.
    """
    transfers: list[dict[str, Any]] = []
    start = 0
    seen_page_sizes = set()

    for _ in range(200):  # Sicherheitslimit gegen Endlosschleifen
        resp = requests.get(
            f"{BASE_URL}/v4/leagues/{league_id}/managers/{manager_id}/transfer",
            params={"start": start},
            headers=_auth_headers(token),
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  [Warnung] Transferabruf fuer Manager {manager_id} fehlgeschlagen "
                  f"(Status {resp.status_code}) bei start={start}")
            break

        data = resp.json()
        items = data.get("it", [])
        if not items:
            break

        in_season = [t for t in items if t.get("dt", "") >= SEASON_START]
        transfers.extend(in_season)

        if len(in_season) < len(items):
            # Mindestens ein Eintrag auf dieser Seite war schon aelter als
            # SEASON_START -> alles Weitere ist noch aelter, hier aufhoeren.
            break

        seen_page_sizes.add(len(items))
        start += len(items)
        time.sleep(REQUEST_PAUSE_SECONDS)

        # Wenn die Seite kleiner war als die bisher groesste gesehene Seitengroesse,
        # war es vermutlich die letzte Seite.
        if seen_page_sizes and len(items) < max(seen_page_sizes):
            break

    # Sicherheits-Filter: stellt sicher, dass wirklich nur Transfers ab
    # SEASON_START zurueckgegeben werden, auch falls die Annahme "neueste
    # zuerst" doch nicht ueberall zutreffen sollte.
    return [t for t in transfers if t.get("dt", "") >= SEASON_START]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    """Schickt eine Nachricht an einen Telegram-Bot-Chat. Wirft bei Fehlern eine Exception."""
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram-Versand fehlgeschlagen (Status {resp.status_code}): {resp.text[:300]}")


# ---------------------------------------------------------------------------
# Zustand zwischen zwei Laeufen (fuer den dynamisch gemessenen Bonus)
# ---------------------------------------------------------------------------

def transfer_fingerprint(t: dict[str, Any]) -> str:
    """Eindeutiger Fingerabdruck eines Transfer-Datensatzes, um 'neue' Transfers
    seit dem letzten Lauf zu erkennen (unabhaengig von einer echten Transfer-ID,
    die die API nicht liefert)."""
    raw = json.dumps(t, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_state() -> Optional[dict[str, Any]]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state: dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Kalibrierung
# ---------------------------------------------------------------------------

def initial_calibration(
    my_transfers: list[dict[str, Any]],
    my_real_budget: int,
    other_transfers_by_manager: dict[str, list[dict[str, Any]]],
) -> tuple[dict[Any, int], int]:
    """
    Nur beim ALLERERSTEN Lauf (noch kein gespeicherter Zustand vorhanden).

    Ermittelt die plausibelste Vorzeichen-Zuordnung pro Transfer-Typ ('tty':
    Kauf oder Verkauf?). WICHTIG: das laesst sich NICHT allein aus den eigenen
    Daten bestimmen, weil der (unbekannte) Bonus ein freier Parameter ist -
    jede Vorzeichen-Kombination laesst sich mit einem passend gewaehlten Bonus
    exakt an den eigenen echten Kontostand anpassen (das war der Fehler in der
    Vorgaengerversion). Stattdessen nutzen wir die GESAMTE LIGA: fuer jede
    moegliche Kombination berechnen wir die geschaetzten Kontostaende ALLER
    anderen Teilnehmer und waehlen die Kombination, bei der diese Schaetzungen
    am plausibelsten zusammen liegen (kleinste Streuung/Spannweite) - in einer
    jungen Liga mit gleichem Startbudget sollten die Kontostaende nicht um
    hunderte Millionen Euro auseinanderliegen.

    Ab dem naechsten Lauf wird der Bonus dann nicht mehr geschaetzt, sondern
    live aus der Differenz deines echten Kontostands gemessen (siehe main()).
    Die Vorzeichen-Zuordnung selbst bleibt danach unveraendert (Annahme:
    strukturelle API-Eigenschaft, aendert sich nicht im Saisonverlauf).
    """
    distinct_types = sorted({t.get("tty") for t in my_transfers if "tty" in t})
    if not distinct_types:
        # Noch keine eigenen Transfers - Vorzeichen noch nicht bestimmbar,
        # aber auch noch nicht relevant.
        return {}, max(my_real_budget - STARTBUDGET, 0)

    candidates = []
    for signs in itertools.product([1, -1], repeat=len(distinct_types)):
        mapping = dict(zip(distinct_types, signs))
        my_signed_sum = sum(mapping.get(t.get("tty"), 0) * t.get("trp", 0) for t in my_transfers)
        implied_bonus = my_real_budget - STARTBUDGET - my_signed_sum

        other_budgets = [
            estimate_budget(transfers, mapping, implied_bonus)
            for transfers in other_transfers_by_manager.values()
        ]
        spread = (max(other_budgets) - min(other_budgets)) if other_budgets else 0
        candidates.append((mapping, implied_bonus, spread, other_budgets))

    print("  Kandidaten fuer Vorzeichen-Zuordnung "
          "(tty -> Vorzeichen | impliziter Bonus | Streuung ueber die Liga):")
    for mapping, implied_bonus, spread, _ in candidates:
        bonus_str = f"{implied_bonus:,.0f}".replace(",", ".")
        spread_str = f"{spread:,.0f}".replace(",", ".")
        print(f"    {mapping} -> Bonus {bonus_str} EUR | Streuung {spread_str} EUR")

    # Wichtiger Sonderfall: eine Vorzeichen-Kombination und ihre komplette
    # Umkehrung (z.B. {1:+1,2:-1} vs. {1:-1,2:+1}) liefern IMMER exakt dieselbe
    # Streuung (mathematisch beweisbar - die Umkehrung spiegelt nur alle
    # Abweichungen um deinen eigenen Kontostand, die Spannweite bleibt gleich).
    # Bei einem Gleichstand entscheidet deshalb zusaetzlich die Plausibilitaet
    # des Bonus: ein Bonus sollte nicht negativ sein.
    min_spread = min(c[2] for c in candidates)
    tied = [c for c in candidates if c[2] == min_spread]
    if len(tied) > 1:
        non_negative = [c for c in tied if c[1] >= 0]
        pool = non_negative if non_negative else tied
        best_mapping, best_bonus, best_spread, _ = min(pool, key=lambda c: abs(c[1]))
        print(f"  [Hinweis] {len(tied)} Kandidaten mit identischer Streuung gefunden - "
              "Bonus-Plausibilitaet (nicht negativ) hat entschieden.")
    else:
        best_mapping, best_bonus, best_spread, _ = tied[0]

    print(f"  -> Gewaehlt: {best_mapping} -> "
          f"Bonus {best_bonus:,.0f} EUR, Streuung {best_spread:,.0f} EUR".replace(",", "."))
    print("  [Hinweis] Bitte beim allerersten Lauf kurz gegenchecken, ob die resultierenden")
    print("  Kontostaende in der Tabelle unten plausibel wirken (aehnliche Groessenordnung,")
    print("  keine utopischen Werte). Falls nicht, sag mir die Kandidatenliste oben.")

    return best_mapping, best_bonus


def estimate_budget(
    transfers: list[dict[str, Any]], sign_mapping: dict[Any, int], total_bonus: int
) -> int:
    total = STARTBUDGET + total_bonus
    for t in transfers:
        tty = t.get("tty")
        sign = sign_mapping.get(tty)
        if sign is None:
            print(f"  [Warnung] Unbekannter Transfer-Typ tty={tty} bei Transfer {t} - wird ignoriert.")
            continue
        total += sign * t.get("trp", 0)
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    email = os.environ.get("KICKBASE_EMAIL")
    password = os.environ.get("KICKBASE_PASSWORD")
    if not email or not password:
        print("Bitte KICKBASE_EMAIL und KICKBASE_PASSWORD als Umgebungsvariablen setzen.")
        sys.exit(1)

    print("Logge ein ...")
    login_data = login(email, password)
    token = login_data["tkn"]
    my_user_id = login_data["u"]["id"]

    league_id = find_league_id(login_data, LEAGUE_NAME)
    print(f"Liga gefunden: {LEAGUE_NAME} ({league_id})")

    print("Hole Rangliste (Namen, Teamwert, Punkte) ...")
    ranking = get_ranking(token, league_id)
    managers = ranking.get("us", [])
    if not managers:
        raise RuntimeError("Keine Manager in der Rangliste gefunden - Response-Struktur hat sich evtl. geaendert.")

    print("Hole deinen echten Kontostand ...")
    my_real_budget = get_my_budget(token, league_id)
    if my_real_budget is None:
        raise RuntimeError("Konnte /me/budget nicht auslesen.")
    print(f"  Dein echter Kontostand lt. Kickbase: {my_real_budget:,.0f} EUR".replace(",", "."))

    print("Hole deine eigene Transferhistorie ...")
    my_transfers = get_manager_transfers(token, league_id, my_user_id)
    my_fingerprints = {transfer_fingerprint(t) for t in my_transfers}

    print("Hole Transferhistorie aller anderen Teilnehmer ...")
    other_transfers_by_manager: dict[str, list[dict[str, Any]]] = {}
    for m in managers:
        manager_id = m.get("i")
        if manager_id == my_user_id:
            continue
        other_transfers_by_manager[manager_id] = get_manager_transfers(token, league_id, manager_id)

    state = load_state()

    if state is None:
        print("Kein gespeicherter Zustand gefunden (erster Lauf) - fuehre Anfangs-Kalibrierung durch ...")
        sign_mapping, known_total_bonus = initial_calibration(my_transfers, my_real_budget, other_transfers_by_manager)
    else:
        print("Gespeicherten Zustand aus letztem Lauf geladen - messe Bonus-Zuwachs live ...")
        sign_mapping = dict(state.get("sign_mapping", []))
        known_total_bonus = state.get("known_total_bonus", 0)
        old_fingerprints = set(state.get("my_transfer_fingerprints", []))
        old_real_budget = state.get("my_real_budget", my_real_budget)

        new_transfers = [t for t in my_transfers if transfer_fingerprint(t) not in old_fingerprints]
        net_new_transfers = sum(
            sign_mapping.get(t.get("tty"), 0) * t.get("trp", 0) for t in new_transfers
        )
        bonus_delta = (my_real_budget - old_real_budget) - net_new_transfers
        known_total_bonus += bonus_delta

        print(f"  {len(new_transfers)} neue eigene Transfer(s) seit letztem Lauf.")
        print(f"  Bonus-Zuwachs seit letztem Lauf: {bonus_delta:,.0f} EUR".replace(",", "."))
        print(f"  Neuer Gesamt-Bonus (dynamisch, seit Ligastart): {known_total_bonus:,.0f} EUR".replace(",", "."))

    print("\nErstelle Ergebnis-Tabelle ...\n")
    results = []
    for m in managers:
        manager_id = m.get("i")
        name = m.get("n", "?")
        team_value = m.get("tv")
        points = m.get("sp")

        if manager_id == my_user_id:
            est_budget = my_real_budget
            note = "(echter Wert)"
        else:
            transfers = other_transfers_by_manager.get(manager_id, [])
            est_budget = estimate_budget(transfers, sign_mapping, known_total_bonus)
            note = "(geschaetzt)"

        results.append({
            "name": name,
            "budget": est_budget,
            "team_value": team_value,
            "points": points,
            "note": note,
        })

    results.sort(key=lambda r: r["budget"], reverse=True)

    # --- Konsolen-Ausgabe (landet z.B. im GitHub Actions Log) ---------------
    console_lines = [f"{'Name':<25} {'Kontostand':>16}  {'Teamwert':>16}  {'Punkte':>8}", "-" * 72]
    for r in results:
        budget_str = f"{r['budget']:,.0f}".replace(",", ".")
        tv_str = f"{r['team_value']:,.0f}".replace(",", ".") if r["team_value"] is not None else "?"
        pts_str = str(r["points"]) if r["points"] is not None else "?"
        console_lines.append(f"{r['name']:<25} {budget_str:>16} {r['note']:<12} {tv_str:>16}  {pts_str:>8}")
    print("\n" + "\n".join(console_lines))

    # --- Telegram-Nachricht (optional) ---------------------------------------
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        timestamp = time.strftime("%d.%m.%Y %H:%M")
        msg_lines = [f"<b>Kickbase Kontostand - {LEAGUE_NAME}</b>", f"Stand: {timestamp} Uhr", ""]
        for r in results:
            budget_str = f"{r['budget']:,.0f}".replace(",", ".")
            marker = " (echt)" if r["note"].startswith("(echter") else ""
            msg_lines.append(f"{r['name']}: {budget_str} EUR{marker}")
        message = "\n".join(msg_lines)
        try:
            send_telegram_message(bot_token, chat_id, message)
            print("\nTelegram-Nachricht erfolgreich gesendet.")
        except RuntimeError as exc:
            print(f"\n[Warnung] {exc}")
    else:
        print("\n[Hinweis] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt - keine Telegram-Nachricht gesendet.")

    # --- Zustand fuer den naechsten Lauf speichern (fuer die dynamische Bonus-Messung) ---
    save_state({
        "sign_mapping": list(sign_mapping.items()),
        "known_total_bonus": known_total_bonus,
        "my_real_budget": my_real_budget,
        "my_transfer_fingerprints": list(my_fingerprints),
    })


if __name__ == "__main__":
    main()
