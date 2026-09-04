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

import itertools
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# CONFIG - hier anpassen
# ---------------------------------------------------------------------------

LEAGUE_ID = "5567806"                  # stabile ID eurer Liga - aendert sich bei einer
                                       # Umbenennung NICHT und wird deshalb zuerst gesucht
LEAGUE_NAME = "TSV Tiefenbach 26/27"   # nur Rueckfallebene, falls die ID mal nicht passt
STARTBUDGET = 50_000_000              # Startbudget lt. Liga-Einstellungen (das aendert sich normalerweise nicht)
SEASON_START = "2026-08-03T00:00:00Z" # Datum des Kontostand-Resets (03.08., Uhrzeit unbekannt ->
                                       # sicherheitshalber Tagesbeginn UTC). Transfers VOR diesem
                                       # Zeitpunkt werden ignoriert (gehoeren zur Vorsaison).

BASE_URL = "https://api.kickbase.com"
REQUEST_PAUSE_SECONDS = 0.4           # kleine Pause zwischen Requests, um die API nicht zu stressen


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


def find_league_id(login_data: dict[str, Any], league_name: str) -> tuple[str, str]:
    """
    Findet eure Liga - zuerst ueber die stabile LEAGUE_ID, ersatzweise ueber den
    Namen. Kickbase benennt Ligen zum Saisonwechsel um (z.B. "25/26" -> "26/27"),
    die ID bleibt dabei gleich. Deshalb hat die ID Vorrang: so laeuft das Skript
    auch nach einer Umbenennung ohne Anpassung weiter.

    Rueckgabe: (liga_id, aktueller_liganame)
    """
    leagues = login_data.get("srvl", [])

    for lg in leagues:
        if str(lg.get("id", "")) == str(LEAGUE_ID):
            aktueller_name = lg.get("name", LEAGUE_NAME)
            if aktueller_name.strip().lower() != league_name.strip().lower():
                print(f"  [Hinweis] Liga heisst inzwischen {aktueller_name!r} "
                      f"(im Skript steht {league_name!r}) - ID passt, laeuft weiter.")
            return str(lg["id"]), aktueller_name

    for lg in leagues:
        if lg.get("name", "").strip().lower() == league_name.strip().lower():
            print(f"  [Hinweis] Liga ueber den Namen gefunden, ID lautet {lg.get('id')} "
                  f"- bitte LEAGUE_ID im Skript darauf anpassen.")
            return str(lg["id"]), lg.get("name", league_name)

    available = [f"{lg.get('name')} (ID {lg.get('id')})" for lg in leagues]
    raise ValueError(
        f"Weder LEAGUE_ID {LEAGUE_ID} noch Liga '{league_name}' in deinem Account gefunden.\n"
        f"Verfuegbare Ligen: {available}\n"
        f"-> Passe LEAGUE_ID (bevorzugt) oder LEAGUE_NAME in den CONFIG-Einstellungen an."
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


def get_manager_transfers(
    token: str, league_id: str, manager_id: str, apply_season_filter: bool = True
) -> list[dict[str, Any]]:
    """
    Holt die (paginierte) Transferhistorie eines Managers.

    apply_season_filter=True (Standard): alles vor SEASON_START wird verworfen
    (Transfers aus einer Vorsaison, die den aktuellen Kontostand nicht mehr
    beeinflussen). Die Eintraege kommen absteigend chronologisch (neueste
    zuerst) - sobald der erste Eintrag vor SEASON_START auftaucht, koennen wir
    das Blaettern abbrechen und sparen unnoetige Anfragen an die API.

    apply_season_filter=False: die komplette Historie ueber alle Saisons. Das
    wird fuer die Kauf/Verkauf-Bestimmung gebraucht, weil ein Spieler, den du
    heute besitzt, durchaus schon in der Vorsaison gekauft worden sein kann.
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

        if apply_season_filter:
            in_season = [t for t in items if t.get("dt", "") >= SEASON_START]
            transfers.extend(in_season)
            if len(in_season) < len(items):
                # Mindestens ein Eintrag auf dieser Seite war schon aelter als
                # SEASON_START -> alles Weitere ist noch aelter, hier aufhoeren.
                break
        else:
            transfers.extend(items)

        seen_page_sizes.add(len(items))
        start += len(items)
        time.sleep(REQUEST_PAUSE_SECONDS)

        # Wenn die Seite kleiner war als die bisher groesste gesehene Seitengroesse,
        # war es vermutlich die letzte Seite.
        if seen_page_sizes and len(items) < max(seen_page_sizes):
            break

    if apply_season_filter:
        # Sicherheits-Filter: stellt sicher, dass wirklich nur Transfers ab
        # SEASON_START zurueckgegeben werden, auch falls die Annahme "neueste
        # zuerst" doch nicht ueberall zutreffen sollte.
        return [t for t in transfers if t.get("dt", "") >= SEASON_START]
    return transfers


def get_squad_player_ids(token: str, league_id: str, manager_id: str) -> set[str]:
    """Liefert die Spieler-IDs, die dieser Manager AKTUELL besitzt."""
    resp = requests.get(
        f"{BASE_URL}/v4/leagues/{league_id}/managers/{manager_id}/squad",
        headers=_auth_headers(token),
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  [Warnung] Kaderabruf fehlgeschlagen (Status {resp.status_code})")
        return set()
    return {p.get("pi") for p in resp.json().get("it", []) if p.get("pi")}


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
# Kalibrierung
# ---------------------------------------------------------------------------

def determine_signs_by_ownership(
    full_transfers: list[dict[str, Any]], owned_player_ids: set[str]
) -> Optional[dict[Any, int]]:
    """
    Bestimmt BEWEISBAR, welcher tty-Wert ein Kauf und welcher ein Verkauf ist.

    Logik: Wenn ein Spieler HEUTE in deinem Kader steht, dann muss dein
    juengster Transfer mit genau diesem Spieler ein KAUF gewesen sein - sonst
    wuerde er dir ja nicht gehoeren. Umgekehrt: gehoert er dir heute nicht mehr,
    obwohl er in deiner Historie auftaucht, war dein juengster Transfer mit ihm
    ein VERKAUF.

    Beide Richtungen liefern unabhaengige Belege. Stimmen sie ueberein, ist die
    Zuordnung eindeutig bewiesen - ganz ohne statistische Annahmen.

    Rueckgabe: {tty_kauf: -1, tty_verkauf: +1} (Kauf kostet Geld, Verkauf bringt
    Geld) oder None, wenn sich keine eindeutige Aussage treffen laesst.
    """
    # Juengster Transfer je Spieler (Liste ist neueste-zuerst sortiert)
    latest_per_player: dict[str, dict[str, Any]] = {}
    for t in full_transfers:
        pi = t.get("pi")
        if pi and pi not in latest_per_player:
            latest_per_player[pi] = t

    buy_votes: dict[Any, int] = {}
    sell_votes: dict[Any, int] = {}
    buy_examples: list[str] = []
    sell_examples: list[str] = []

    for pi, t in latest_per_player.items():
        tty = t.get("tty")
        if tty is None:
            continue
        name = t.get("pn", f"Spieler {pi}")
        betrag = t.get("trp", 0)
        datum = (t.get("dt") or "")[:10]
        beleg = f"{name}: {betrag:,.0f} EUR am {datum} (tty={tty})".replace(",", ".")
        if pi in owned_player_ids:
            buy_votes[tty] = buy_votes.get(tty, 0) + 1
            if len(buy_examples) < 3:
                buy_examples.append(beleg)
        else:
            sell_votes[tty] = sell_votes.get(tty, 0) + 1
            if len(sell_examples) < 3:
                sell_examples.append(beleg)

    print(f"  Belege KAUF  (Spieler heute im Kader):     {dict(buy_votes)}")
    for b in buy_examples:
        print(f"     z.B. {b}")
    print(f"  Belege VERKAUF (Spieler heute nicht mehr da): {dict(sell_votes)}")
    for b in sell_examples:
        print(f"     z.B. {b}")

    if not buy_votes:
        print("  [!] Keine Kauf-Belege gefunden (kein Spieler aus der Historie im aktuellen Kader).")
        return None

    buy_tty = max(buy_votes, key=lambda k: buy_votes[k])

    # Sauberkeitspruefung: der als "Kauf" bestimmte Typ sollte bei den
    # Verkaufs-Belegen deutlich seltener vorkommen als der andere Typ.
    if len(buy_votes) > 1:
        anteil = buy_votes[buy_tty] / sum(buy_votes.values())
        print(f"  [!] Achtung: die Kauf-Belege sind nicht eindeutig "
              f"({anteil:.0%} entfallen auf tty={buy_tty}).")
        if anteil < 0.8:
            print("  [!] Zu uneindeutig - falle auf das statistische Verfahren zurueck.")
            return None

    sell_candidates = [k for k in list(buy_votes) + list(sell_votes) if k != buy_tty]
    if not sell_candidates:
        print("  [!] Nur ein einziger Transfer-Typ vorhanden - Verkaufs-Typ unbekannt.")
        return None

    sell_tty = max(set(sell_candidates), key=lambda k: sell_votes.get(k, 0))

    mapping = {buy_tty: -1, sell_tty: 1}
    print(f"  ==> BEWIESEN: tty={buy_tty} ist KAUF (Geld ab), "
          f"tty={sell_tty} ist VERKAUF (Geld drauf).")
    return mapping


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

    league_id, league_display_name = find_league_id(login_data, LEAGUE_NAME)
    print(f"Liga gefunden: {league_display_name} ({league_id})")

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

    print("Bestimme Kauf/Verkauf anhand deines aktuellen Kaders ...")
    my_full_transfers = get_manager_transfers(token, league_id, my_user_id, apply_season_filter=False)
    my_owned = get_squad_player_ids(token, league_id, my_user_id)
    print(f"  Dein Kader umfasst aktuell {len(my_owned)} Spieler; "
          f"deine Historie {len(my_full_transfers)} Transfers (alle Saisons).")
    proven_mapping = determine_signs_by_ownership(my_full_transfers, my_owned)

    # --- Pruefung des Saison-Stichtags --------------------------------------
    # Wenn der Budget-Reset am 03.08. z.B. erst um 8 Uhr war, gehoeren Transfers
    # vom Vormittag noch zur ALTEN Saison und duerfen nicht mitgerechnet werden.
    # Deshalb hier alle Transfers rund um den Stichtag mit Uhrzeit auflisten.
    stichtag = SEASON_START[:10]
    tag_davor = (datetime.fromisoformat(stichtag) - timedelta(days=1)).strftime("%Y-%m-%d")
    tag_danach = (datetime.fromisoformat(stichtag) + timedelta(days=1)).strftime("%Y-%m-%d")
    umfeld = sorted(
        (t for t in my_full_transfers if tag_davor <= (t.get("dt") or "")[:10] <= tag_danach),
        key=lambda t: t.get("dt") or "",
    )
    print(f"\n  DEINE Transfers vom {tag_davor} bis {tag_danach} (Stichtag ist {stichtag}):")
    if not umfeld:
        print("    (keine) - dann ist die Uhrzeit des Stichtags unkritisch.")
    else:
        for t in umfeld:
            typ = "KAUF   " if proven_mapping and proven_mapping.get(t.get("tty")) == -1 else "VERKAUF"
            gezaehlt = "GEZAEHLT" if (t.get("dt") or "") >= SEASON_START else "ignoriert"
            print(f"    {(t.get('dt') or '')[:16].replace('T', ' ')}  {typ}  "
                  f"{t.get('trp', 0):>12,.0f} EUR  {t.get('pn', '?'):<15} [{gezaehlt}]".replace(",", "."))
        print("    >>> Falls hier Eintraege VOR dem Reset mit [GEZAEHLT] stehen, muss")
        print("        SEASON_START oben im Skript auf die echte Uhrzeit gesetzt werden.")

    print("Hole Transferhistorie aller anderen Teilnehmer ...")
    other_transfers_by_manager: dict[str, list[dict[str, Any]]] = {}
    for m in managers:
        manager_id = m.get("i")
        if manager_id == my_user_id:
            continue
        other_transfers_by_manager[manager_id] = get_manager_transfers(token, league_id, manager_id)

    if proven_mapping is not None:
        sign_mapping = proven_mapping
        my_signed_sum = sum(
            sign_mapping.get(t.get("tty"), 0) * t.get("trp", 0) for t in my_transfers
        )
        known_total_bonus = my_real_budget - STARTBUDGET - my_signed_sum
        print(f"Aufgelaufener Bonus (aus deinem echten Kontostand abgeleitet): "
              f"{known_total_bonus:,.0f} EUR".replace(",", "."))
        # Kontrollrechnung fuer deinen eigenen Kontostand - hier kennen wir die
        # Wahrheit, also muss die Formel exakt aufgehen.
        eigene_kaeufe = sum(t.get("trp", 0) for t in my_transfers if sign_mapping.get(t.get("tty")) == -1)
        eigene_verkaeufe = sum(t.get("trp", 0) for t in my_transfers if sign_mapping.get(t.get("tty")) == 1)
        print(f"  Kontrolle an DIR SELBST (Saison ab {SEASON_START[:10]}):")
        print(f"    Startbudget:            {STARTBUDGET:>15,.0f} EUR".replace(",", "."))
        print(f"    - Kaeufe:               {eigene_kaeufe:>15,.0f} EUR".replace(",", "."))
        print(f"    + Verkaeufe:            {eigene_verkaeufe:>15,.0f} EUR".replace(",", "."))
        print(f"    + Bonus (Restgroesse):  {known_total_bonus:>15,.0f} EUR".replace(",", "."))
        print(f"    = dein echter Stand:    {my_real_budget:>15,.0f} EUR".replace(",", "."))
        print("    >>> Vergleiche diese Kauf-/Verkaufssummen mit deiner Kickbase-App.")
        print("        Stimmen sie nicht, liegt der Fehler in den Rohdaten, nicht in der Formel.")
    else:
        print("Kauf/Verkauf liess sich nicht beweisen - nutze das statistische Verfahren ...")
        sign_mapping, known_total_bonus = initial_calibration(
            my_transfers, my_real_budget, other_transfers_by_manager
        )

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
            breakdown = None
        else:
            transfers = other_transfers_by_manager.get(manager_id, [])
            est_budget = estimate_budget(transfers, sign_mapping, known_total_bonus)
            note = "(geschaetzt)"
            kaeufe = sum(t.get("trp", 0) for t in transfers if sign_mapping.get(t.get("tty")) == -1)
            verkaeufe = sum(t.get("trp", 0) for t in transfers if sign_mapping.get(t.get("tty")) == 1)
            n_kauf = sum(1 for t in transfers if sign_mapping.get(t.get("tty")) == -1)
            n_verkauf = sum(1 for t in transfers if sign_mapping.get(t.get("tty")) == 1)
            breakdown = (n_kauf, kaeufe, n_verkauf, verkaeufe)

        results.append({
            "name": name,
            "budget": est_budget,
            "team_value": team_value,
            "points": points,
            "note": note,
            "breakdown": breakdown,
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

    # --- Aufschluesselung zum manuellen Gegenrechnen --------------------------
    print("\n\nAUFSCHLUESSELUNG (zum Abgleich mit deiner manuellen Auswertung)")
    print(f"Formel je Teilnehmer: {STARTBUDGET:,.0f} - Kaeufe + Verkaeufe + Bonus "
          f"{known_total_bonus:,.0f}".replace(",", "."))
    print(f"{'Name':<20} {'Anz':>4} {'Kaeufe':>16} {'Anz':>4} {'Verkaeufe':>16} {'= Kontostand':>16}")
    print("-" * 82)
    for r in results:
        if not r["breakdown"]:
            print(f"{r['name']:<20} {'':>4} {'(dein echter Wert)':>16} "
                  f"{'':>4} {'':>16} {r['budget']:>16,.0f}".replace(",", "."))
            continue
        n_k, kaeufe, n_v, verkaeufe = r["breakdown"]
        print(f"{r['name']:<20} {n_k:>4} {kaeufe:>16,.0f} {n_v:>4} {verkaeufe:>16,.0f} "
              f"{r['budget']:>16,.0f}".replace(",", "."))

    # --- Telegram-Nachricht (optional) ---------------------------------------
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        timestamp = time.strftime("%d.%m.%Y %H:%M")
        msg_lines = [f"<b>Kickbase Kontostand - {league_display_name}</b>", f"Stand: {timestamp} Uhr", ""]
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


if __name__ == "__main__":
    main()
