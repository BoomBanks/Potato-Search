#!/usr/bin/env python3
"""
MTG Spike Tool – alles in einem:
  * Spikes (heute + Verlauf 30 Tage + 7-Tage-Ansicht)
  * Hype-Radar (steigende Aufmerksamkeit laut EDHREC-Rang, Ampel im Abgleich zum US-Preis)
  * Kandidaten (US-Preis steigt, Cardmarket hinkt hinterher, geschätzter Gewinn >= 5 EUR)
  * Daten für Watchlist (prices.json)
Läuft 1x täglich über GitHub Actions. Datenquelle: Scryfall (USD = TCGplayer, EUR = Cardmarket).
"""
import datetime as dt
import gzip
import json
import os

import ijson
import requests

# ------------------------- Einstellungen -------------------------
RARITIES = {"rare", "mythic"}   # nur Rares/Mythics
MIN_PRICE_USD = 3.0             # Preis nach Spike mindestens $3
MIN_CHANGE = 0.20               # mindestens +20 %
HISTORY_DAYS = 8                # so viele Tage Preis-Schnappschüsse aufheben
SPIKELOG_DAYS = 30              # Spike-Verlauf: 30 Tage (keine Lücken, wenn du länger nicht reinschaust)
CM_FEE = 0.05                   # Cardmarket-Verkaufsgebühr (ca. 5 %)
COSTS_EUR = 1.50                # Versand/Nebenkosten je Karte (Schätzung)
MIN_PROFIT_EUR = 5.0            # Kandidat erst ab 5 EUR geschätztem Netto-Gewinn
CATCHUP = 0.80                  # Annahme: EU-Preis zieht auf 80 % des US-Preises (in EUR) nach
HYPE_MIN_IMPROVE = 0.20         # EDHREC-Rang mind. 20 % besser als vor ~7 Tagen
HYPE_MIN_PLACES = 200           # ... und mind. 200 Plätze nach vorne
HYPE_MAX_RANK = 15000           # nur Karten, die schon halbwegs gespielt werden
STORE = "store"                 # wird im Branch "daten" aufbewahrt
SITE = "site"                   # wird als Webseite veröffentlicht
UA = {"User-Agent": "MTGSpikeTool/2.0", "Accept": "application/json"}
# -----------------------------------------------------------------


def load(path, default):
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj, gz=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    opener = gzip.open if gz else open
    with opener(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def num(x):
    try:
        v = round(float(x), 2)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def pct(new, old):
    if not new or not old:
        return None
    return round((new - old) / old, 3)


def get_fx():
    for url in ("https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR",
                "https://api.frankfurter.app/latest?from=USD&to=EUR"):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            return float(r.json()["rates"]["EUR"])
        except Exception:
            pass
    return 0.86  # Notfall-Wert


def download_bulk(path="cards.json"):
    r = requests.get("https://api.scryfall.com/bulk-data", headers=UA, timeout=60)
    print("Scryfall-Antwort:", r.status_code)
    r.raise_for_status()
    entries = r.json().get("data", [])
    entry = next((e for e in entries if e.get("type") == "default_cards"), None)
    if entry is None:
        raise SystemExit("Scryfall: Eintrag default_cards fehlt. Typen: " + str([e.get("type") for e in entries]))

    def find_url(obj):
        for k, v in obj.items():
            if "download" in k and isinstance(v, str) and v.startswith("http"):
                return v
        return None

    url = find_url(entry)
    if not url and entry.get("uri"):  # Detailseite des Eintrags nachladen
        url = find_url(requests.get(entry["uri"], headers=UA, timeout=60).json())
    if not url:
        raise SystemExit("Scryfall: Download-Link nicht gefunden. Eintrag: " + json.dumps(entry)[:800])
    print("Lade Kartendatei:", url)
    with requests.get(url, headers=UA, stream=True, timeout=900) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


def cm_link(url):
    """Cardmarket-Link mit Filter: Englisch, Zustand ab Excellent."""
    if not url:
        return None
    return url + ("&" if "?" in url else "?") + "language=1&minCondition=3"


def parse(path):
    cards, snap, by_oracle = {}, {}, {}
    with open(path, "rb") as f:
        for c in ijson.items(f, "item", use_float=True):
            if c.get("lang") != "en" or c.get("rarity") not in RARITIES or c.get("digital"):
                continue
            p = c.get("prices") or {}
            usd, usdf, eur, eurf = (num(p.get(k)) for k in ("usd", "usd_foil", "eur", "eur_foil"))
            if not any((usd, usdf, eur, eurf)):
                continue
            rank = c.get("edhrec_rank")
            snap[c["id"]] = [usd, usdf, eur, eurf, rank]
            faces = c.get("card_faces") or [{}]
            img = (c.get("image_uris") or {}).get("small") or (faces[0].get("image_uris") or {}).get("small")
            oid = c.get("oracle_id") or c["id"]
            cards[c["id"]] = {
                "n": c.get("name"), "s": c.get("set_name"), "r": c.get("rarity", "?")[0].upper(),
                "o": oid, "sf": c.get("scryfall_uri"),
                "cm": cm_link((c.get("purchase_uris") or {}).get("cardmarket")),
                "ed": (c.get("related_uris") or {}).get("edhrec"), "img": img,
            }
            by_oracle.setdefault(oid, []).append(c["id"])
    return cards, snap, by_oracle


def pick_base(hist, today, days_back):
    """Schnappschuss, der am nächsten an 'vor X Tagen' liegt (aber vor heute)."""
    dates = sorted(d for d in hist if d < today)
    if not dates:
        return None, {}
    if days_back == 1:
        return dates[-1], hist[dates[-1]]
    target = (dt.date.fromisoformat(today) - dt.timedelta(days=days_back)).isoformat()
    best = min(dates, key=lambda d: abs((dt.date.fromisoformat(d) - dt.date.fromisoformat(target)).days))
    return best, hist[best]


def main():
    today = os.environ.get("TODAY") or dt.date.today().isoformat()
    fx = get_fx()
    path = os.environ.get("BULK_FILE") or download_bulk()
    cards, snap, by_oracle = parse(path)
    print(f"{len(snap)} Karten eingelesen, Kurs 1 USD = {fx} EUR")

    hist = load(f"{STORE}/history.json.gz", {})
    hist.pop(today, None)
    d1, base1 = pick_base(hist, today, 1)
    d7, base7 = pick_base(hist, today, 7)
    empty = [None] * 5

    def make(cid, foil, base):
        s, b, w = snap[cid], base.get(cid) or empty, base7.get(cid) or empty
        ui, ei = (1, 3) if foil else (0, 2)
        new, old = s[ui], b[ui]
        eur_now, eur_old = s[ei], b[ei]
        rank_now, rank_old = s[4], w[4]
        att = pct(rank_old, rank_now) if rank_now and rank_old else None  # >0 = Aufmerksamkeit steigt
        eu = pct(eur_now, eur_old)
        if att is None and eu is None:
            light = "y"
        elif (att or 0) >= 0.10 or (eu or 0) >= 0.10:
            light = "g"
        else:
            light = "r"
        profit = None
        if eur_now and new:
            profit = round(new * fx * CATCHUP * (1 - CM_FEE) - COSTS_EUR - eur_now, 2)
        e = dict(cards[cid])
        e.update({"id": cid, "f": foil, "old": old, "new": new, "ch": pct(new, old),
                  "eur": eur_now, "euch": eu, "rk": rank_now, "att": att, "l": light, "p": profit})
        return e

    def spikes_vs(base):
        out = []
        for cid, s in snap.items():
            b = base.get(cid)
            if not b:
                continue
            for foil, i in ((False, 0), (True, 1)):
                new, old = s[i], b[i]
                if new and old and new >= MIN_PRICE_USD and (new - old) / old >= MIN_CHANGE:
                    out.append(make(cid, foil, base))
        return sorted(out, key=lambda e: -e["ch"])

    today_spikes = spikes_vs(base1) if base1 else []
    week = spikes_vs(base7)[:300] if base7 else []

    # Spike-Verlauf (30 Tage)
    log = load(f"{STORE}/spikelog.json", [])
    log = [e for e in log if e.get("d") != today]
    for e in today_spikes:
        log.append(dict(e, d=today))
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=SPIKELOG_DAYS)).isoformat()
    log = [e for e in log if e["d"] >= cutoff]
    log.sort(key=lambda e: (e["d"], e["ch"] or 0), reverse=True)

    # Kandidaten: US steigt, EU hinkt hinterher, Gewinn >= 5 EUR, Ampel nicht rot
    cand, seen = [], set()
    for e in today_spikes + week:
        k = (e["id"], e["f"])
        if k in seen or e["l"] == "r" or e["p"] is None or e["p"] < MIN_PROFIT_EUR:
            continue
        if e["euch"] is not None and e["ch"] and e["euch"] > e["ch"] / 2:
            continue  # Cardmarket ist schon nachgezogen
        seen.add(k)
        cand.append(e)
    cand.sort(key=lambda e: -e["p"])

    # Hype-Radar
    hype = []
    if base7:
        for oid, ids in by_oracle.items():
            rank_now = snap[ids[0]][4]
            olds = [base7[i][4] for i in ids if i in base7 and base7[i][4]]
            if not rank_now or not olds or rank_now > HYPE_MAX_RANK:
                continue
            rank_old = olds[0]
            imp = (rank_old - rank_now) / rank_old
            if imp < HYPE_MIN_IMPROVE or rank_old - rank_now < HYPE_MIN_PLACES:
                continue
            with_eur = [i for i in ids if snap[i][2]]
            rep = min(with_eur, key=lambda i: snap[i][2]) if with_eur else ids[0]
            e = make(rep, False, base7)
            usd_ch = e["ch"]
            e["l"] = "g" if (usd_ch or 0) >= 0.05 else ("r" if (usd_ch or 0) <= -0.10 else "y")
            e.update({"rk_old": rank_old, "att": round(imp, 3)})
            hype.append(e)
        hype.sort(key=lambda e: -e["att"])
        hype = hype[:150]

    # Preise für Watchlist
    prices = {}
    for cid, s in snap.items():
        if max(x or 0 for x in s[:4]) < 1:
            continue
        w = base7.get(cid) or empty
        c = cards[cid]
        prices[cid] = [c["n"], c["s"], s[0], s[1], s[2], s[3], w[0], w[2]]

    hist[today] = snap
    for d in sorted(hist)[:-HISTORY_DAYS]:
        del hist[d]

    save(f"{STORE}/history.json.gz", hist, gz=True)
    save(f"{STORE}/spikelog.json", log)
    save(f"{SITE}/prices.json", prices)
    save(f"{SITE}/data.json", {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "today": today, "base1": d1, "base7": d7, "days": len(hist), "fx": fx,
        "settings": {"min_usd": MIN_PRICE_USD, "min_change": MIN_CHANGE, "fee": CM_FEE,
                     "costs": COSTS_EUR, "min_profit": MIN_PROFIT_EUR, "catchup": CATCHUP},
        "log": log, "week": week, "cand": cand, "hype": hype,
    })
    print(f"Spikes heute: {len(today_spikes)}, 7 Tage: {len(week)}, Kandidaten: {len(cand)}, Hype: {len(hype)}")


if __name__ == "__main__":
    main()
