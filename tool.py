#!/usr/bin/env python3
"""
MTG Spike Tool – alles in einem:
  * Spikes (heute + Verlauf 30 Tage + 7-Tage-Ansicht)
  * Hype-Radar (steigende Aufmerksamkeit laut EDHREC-Rang, Ampel im Abgleich zum US-Preis)
  * Kandidaten (US-Preis steigt, Cardmarket hinkt hinterher, geschätzter Gewinn >= 3 EUR)
  * CM-Spikes (Spikes direkt aus Cardmarket-Preisen, mit US-Vergleich)
  * Daten für Watchlist (prices.json)
Läuft 1x täglich über GitHub Actions. Datenquelle: Scryfall (USD = TCGplayer, EUR = Cardmarket-Trend),
dazu der offizielle Cardmarket-Preis-Führer (Durchschnitt der Verkäufe gestern / 7 / 30 Tage).
"""
import datetime as dt
import gzip
import json
import os
import re
import xml.etree.ElementTree as ET

import ijson
import requests

# ------------------------- Einstellungen -------------------------
RARITIES = {"uncommon", "rare", "mythic"}  # Uncommons, Rares, Mythics (Reserved List: alle Seltenheiten)
MIN_PRICE_USD = 3.0             # Preis nach Spike mindestens $3
MIN_CHANGE = 0.20               # mindestens +20 %
RL_MIN_PRICE_USD = 10.0         # Reserved List: Preis mind. $10
RL_MIN_CHANGE = 0.10            # Reserved List: Tages-Spike ab +10 % ...
RL_MIN_ABS_USD = 10.0           # ... oder ab +$10
RL_WEEK_EXTRA = 0.10            # Reserved List: 7 Tage mind. 10 Prozentpunkte über dem RL-Schnitt
RL_HOLD = 0.90                  # Spike "bestätigt", wenn der Preis am Folgetag noch mind. 90 % hält
HOLD_DAYS = 3                   # Spike "hält", wenn der US-Preis mind. 3 Tage lang ...
HOLD_MIN = 0.80                 # ... jeden Tag bei mind. 80 % des Spike-Preises bleibt -> Ampel Rot wird Gelb
CM_LAG_FACTOR = 3.0             # "CM hinkt stark hinterher": US-Preis (in EUR) mind. 3x so hoch wie die CM-Verkäufe
CM_FOLLOW = 0.20                # "CM zieht schon nach": Verkäufe 7 Tage mind. 20 % über dem 30-Tage-Schnitt
HOLD_LOOKBACK = 14              # haltende Spikes bis 14 Tage zurück dürfen in die Kandidaten
HISTORY_DAYS = 8                # so viele Tage Preis-Schnappschüsse aufheben
SPIKELOG_DAYS = 90              # Spike-Verlauf wird 90 Tage aufbewahrt (für die Ergebnis-Auswertung)
LOG_SHOW_DAYS = 30              # ... auf der Webseite werden davon die letzten 30 Tage gezeigt
OUTCOME_DAYS = (7, 14, 30)      # Ergebnis-Archiv: Preise X Tage nach dem Spike festhalten
CM_FEE = 0.05                   # Cardmarket-Verkaufsgebühr (ca. 5 %)
SHIP_LOW_EUR = 2.00             # Versand beim Einkauf, Karte unter der Grenze (Durchschnitt)
SHIP_HIGH_EUR = 15.49           # Versand ab der Grenze (versichert, z. B. aus Deutschland)
SHIP_LIMIT_EUR = 25.0           # ab diesem Kartenpreis gilt der teure Versand
MIN_PROFIT_EUR = 3.0            # Kandidat erst ab 3 EUR geschätztem Netto-Gewinn
BUY_MARKUP = 0.15               # Einkauf realistisch: Cardmarket-Trend + 15 % (Englisch/EX+, Verkäuferland, Zoll)
# Erwarteter Cardmarket-Preis: EU-Preis VOR dem Spike x (1 + US-Anstieg) – jede Karte mit ihrem eigenen EU/US-Verhältnis
STAPLE_RANK = 2000              # "Staple": EDHREC-Rang bis 2.000 (nur Abzeichen/Filter, zählt nicht für die Ampel)
HYPE_MIN_IMPROVE = 0.20         # EDHREC-Rang mind. 20 % besser als vor ~7 Tagen
HYPE_MIN_PLACES = 200           # ... und mind. 200 Plätze nach vorne
HYPE_MAX_RANK = 15000           # nur Karten, die schon halbwegs gespielt werden
# CM-Spikes: Spikes direkt aus den Cardmarket-Preisen (Trend, EUR)
CM_MIN_EUR = 3.0                # Cardmarket-Preis nach Spike mind. 3 EUR
CM_MIN_CHANGE = 0.20            # mind. +20 %
CM_RL_MIN_EUR = 10.0            # Reserved List: mind. 10 EUR ...
CM_RL_MIN_CHANGE = 0.10         # ... und +10 % ...
CM_RL_MIN_ABS = 10.0            # ... oder +10 EUR
CM_US_CONFIRM = 0.10            # US zieht mit, wenn dort im gleichen Zeitraum mind. +10 %
CM_US_FLAT = 0.03               # US gilt als "ruhig" bis +3 %
CM_HOLD = 0.90                  # Spike hält, wenn der CM-Preis am Folgetag noch mind. 90 % hat
CM_LOG_DAYS = 30                # CM-Spike-Verlauf: 30 Tage
CM_WEEK_MAX = 300               # 7-Tage-Ansicht: höchstens so viele Karten
STORE = "store"                 # wird im Branch "daten" aufbewahrt
SITE = "site"                   # wird als Webseite veröffentlicht
UA = {"User-Agent": "MTGSpikeTool/2.0", "Accept": "application/json"}

# YouTube: neueste Videos dieser Kanäle werden täglich nach Kartennamen durchsucht.
# Eintragen als @Handle (wie in der YouTube-Adresse) oder als Kanal-ID (beginnt mit UC).
YT_CHANNELS = [
    "@MTGGoldfish",
    "@TolarianCommunityCollege",
    "@commandcast",                  # The Command Zone
    "UCTp-iVOtTrKau0skmfZlo5Q",      # Alpha Investments (MTG-Finanz)
    "UC8ZGymAvfP97qJabgqUkz4A",      # Magic: The Gathering (offiziell, Vorschauen/Spoiler)
    "UCApiDwDpn_JpzRGLZKrdr1g",      # EDHRECast (Commander)
    "UCVmqfvWsKeFaMGY68CbcVVQ",      # TCGplayer
]
YT_DAYS = 7                     # Erwähnungen der letzten 7 Tage zählen
YT_DESC_CHARS = 400             # nur den Anfang der Videobeschreibung durchsuchen (weniger Fehltreffer)
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


def cm_guide():
    """Offizieller Cardmarket-Preis-Führer (täglich neu). Fehler -> leer, dann nur Scryfall."""
    try:
        if os.environ.get("CM_GUIDE_FILE"):   # nur zum Testen: Datei statt Download
            pg = load(os.environ["CM_GUIDE_FILE"], {})
            return {x["idProduct"]: x for x in pg.get("priceGuides", [])}
        r = requests.get("https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json",
                         headers=UA, timeout=300)
        r.raise_for_status()
        pg = r.json()
        g = {x["idProduct"]: x for x in pg.get("priceGuides", []) if x.get("idProduct")}
        print(f"Cardmarket-Preis-Führer: {len(g)} Einträge, Stand {pg.get('createdAt')}")
        return g
    except Exception as ex:
        print("Cardmarket-Preis-Führer nicht geladen (nur Scryfall):", ex)
        return {}


def cm_sales(g, foil, has_foil_us):
    """Durchschnitt der Verkäufe [gestern, 7 Tage, 30 Tage] für Normal oder Foil – oder None."""
    if not g or (foil and not has_foil_us):
        return None   # Foil-Werte nur, wenn es die Karte wirklich als Foil gibt
    sfx = "-foil" if foil else ""
    vals = [num(g.get(k + sfx)) for k in ("avg1", "avg7", "avg30")]
    return vals if any(vals) else None


def cm_link(url):
    """Cardmarket-Link mit Filter: Englisch, Zustand ab Excellent."""
    if not url:
        return None
    return url + ("&" if "?" in url else "?") + "language=1&minCondition=3"


def iter_cards(path):
    """Liest die Scryfall-Datei: egal ob gepackt (.gz) oder nicht,
    als große Liste ([...]) oder eine Karte pro Zeile (JSON Lines)."""
    with open(path, "rb") as raw:
        gz = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if gz else open
    with opener(path, "rb") as f:
        first = f.read(64).lstrip()[:1]
    with opener(path, "rb") as f:
        if first == b"[":
            yield from ijson.items(f, "item", use_float=True)
        else:
            for line in f:
                line = line.strip().rstrip(b",")
                if line and line not in (b"[", b"]"):
                    yield json.loads(line)


def parse(path):
    cards, snap, by_oracle = {}, {}, {}
    if True:
        for c in iter_cards(path):
            rl = bool(c.get("reserved"))
            if c.get("lang") != "en" or c.get("digital") or (c.get("rarity") not in RARITIES and not rl):
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
                "o": oid, "sf": c.get("scryfall_uri"), "rl": 1 if rl else 0,
                "cm": cm_link((c.get("purchase_uris") or {}).get("cardmarket")),
                "ed": (c.get("related_uris") or {}).get("edhrec"), "img": img,
                "rd": c.get("released_at"),   # Erscheinungsdatum dieser Druckversion
                "cmid": c.get("cardmarket_id"),
            }
            by_oracle.setdefault(oid, []).append(c["id"])
    return cards, snap, by_oracle


# ------------------------- YouTube -------------------------
YT_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
         "m": "http://search.yahoo.com/mrss/"}
YT_HDR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
          "Accept-Language": "en-US,en;q=0.8", "Cookie": "CONSENT=YES+1; SOCS=CAI"}


YT_ID_PATTERNS = (r"feeds/videos\.xml\?channel_id=(UC[\w-]{22})", r'"externalId":"(UC[\w-]{22})"',
                  r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                  r'itemprop="identifier" content="(UC[\w-]{22})"', r'"browseId":"(UC[\w-]{22})"',
                  r'"channelId":"(UC[\w-]{22})"')


def yt_resolve(handle, cache):
    """@Handle -> Kanal-ID (UC...). Ergebnis wird gespeichert, damit es nur einmal nachgeschaut wird.
    Probiert mehrere Seiten des Kanals, weil YouTube nicht immer dieselbe Seite ausliefert."""
    if handle.startswith("UC"):
        return handle
    if cache.get(handle):
        return cache[handle]
    for suffix in ("", "/videos", "/about"):
        try:
            r = requests.get("https://www.youtube.com/" + handle + suffix, headers=YT_HDR, timeout=30)
        except Exception as ex:
            print(f"YouTube {handle}{suffix}: nicht erreichbar ({ex})")
            continue
        if r.status_code == 404:
            print(f"YouTube {handle}: Kanal existiert nicht (404) – Handle prüfen")
            return None
        for pat in YT_ID_PATTERNS:
            m = re.search(pat, r.text)
            if m:
                cache[handle] = m.group(1)
                return m.group(1)
        print(f"YouTube {handle}{suffix}: keine Kanal-ID gefunden (Status {r.status_code})")
    return None


def yt_videos(channel_id):
    r = requests.get("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id,
                     headers=YT_HDR, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ch = root.findtext("a:title", "", YT_NS)
    out = []
    for e in root.findall("a:entry", YT_NS):
        vid = e.findtext("yt:videoId", "", YT_NS)
        desc = e.findtext("m:group/m:description", "", YT_NS) or ""
        out.append({"v": vid, "t": e.findtext("a:title", "", YT_NS), "ch": ch,
                    "d": (e.findtext("a:published", "", YT_NS) or "")[:10],
                    "txt": desc[:YT_DESC_CHARS]})
    return out


def name_index(names):
    """Kartennamen nach erstem Wort sortiert, damit die Suche im Text schnell geht."""
    idx = {}
    for n in names:
        n = n.split(" // ")[0].strip()
        if not n or not n[0].isupper():
            continue
        if " " not in n and len(n) < 8:   # kurze Einwort-Namen (z. B. "Opt") ergeben zu viele Fehltreffer
            continue
        idx.setdefault(n.split(" ")[0].rstrip(",:"), set()).add(n)
    return {k: sorted(v, key=len, reverse=True) for k, v in idx.items()}


def find_names(text, idx):
    found = set()
    text = text.replace("\u2019", "'")  # typografisches Apostroph vereinheitlichen
    for m in re.finditer(r"[A-Z][\w'\-]*", text):
        for n in idx.get(m.group(0).rstrip(",:"), ()):
            end = m.start() + len(n)
            if text.startswith(n, m.start()) and (end == len(text) or not text[end].isalnum()):
                found.add(n)
                break
    return found


def youtube(names, today):
    """Sammelt Videos der Kanäle und zählt, in wie vielen Videos (7 Tage) eine Karte erwähnt wird."""
    store = load(f"{STORE}/yt.json", {})
    cache, videos = store.get("ids", {}), store.get("videos", {})
    ok, bad = [], []
    for h in YT_CHANNELS:
        cid = yt_resolve(h, cache)
        if not cid:
            bad.append(h)
            continue
        try:
            for v in yt_videos(cid):
                if v["v"]:
                    videos[v["v"]] = v
            ok.append(h)
        except Exception as ex:
            print(f"YouTube {h}: Video-Liste nicht ladbar ({ex})")
            cache.pop(h, None)   # gespeicherte ID evtl. veraltet -> beim nächsten Lauf neu suchen
            bad.append(h)
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=YT_DAYS)).isoformat()
    videos = {k: v for k, v in videos.items() if v.get("d", "") >= cutoff}
    idx = name_index(names)
    hits = {}
    for v in sorted(videos.values(), key=lambda v: v["d"], reverse=True):
        for n in find_names(v["t"] + "\n" + v.get("txt", ""), idx):
            hits.setdefault(n, []).append({"t": v["t"], "u": "https://www.youtube.com/watch?v=" + v["v"],
                                           "ch": v["ch"], "d": v["d"]})
    save(f"{STORE}/yt.json", {"ids": cache, "videos": videos})
    print(f"YouTube: {len(ok)} Kanäle ok, nicht erreichbar: {bad or '-'}; {len(videos)} Videos, {len(hits)} Karten erwähnt")
    return hits, {"ok": ok, "bad": bad, "videos": len(videos)}


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


def is_spike(new, old, rl):
    if not new or not old:
        return False
    if rl:
        return new >= RL_MIN_PRICE_USD and ((new - old) / old >= RL_MIN_CHANGE or new - old >= RL_MIN_ABS_USD)
    return new >= MIN_PRICE_USD and (new - old) / old >= MIN_CHANGE


def is_cm_spike(new, old, rl):
    if not new or not old:
        return False
    if rl:
        return new >= CM_RL_MIN_EUR and ((new - old) / old >= CM_RL_MIN_CHANGE or new - old >= CM_RL_MIN_ABS)
    return new >= CM_MIN_EUR and (new - old) / old >= CM_MIN_CHANGE


def ship_for(price):
    return SHIP_HIGH_EUR if price >= SHIP_LIMIT_EUR else SHIP_LOW_EUR


def estimate(eur_now, eur_old, usd_new, usd_old, fx, normal=None):
    """Gewinn-Schätzung: (erwarteter CM-Preis, Einkaufspreis, Versand, Gewinn) oder Nones.
    normal = Cardmarket-Normalpreis (Verkäufe 30 Tage Ø); fehlt er, zählt der Trend vor dem Spike."""
    eur_old = normal or eur_old
    if not (eur_now and eur_old and usd_new and usd_old):
        return None, None, None, None
    target = eur_old * usd_new / usd_old
    # Deckel: Cardmarket steigt nicht über das US-Niveau (in EUR) – außer es lag schon vorher darüber
    target = round(min(target, max(eur_old, usd_new * fx)), 2)
    buy = round(eur_now * (1 + BUY_MARKUP), 2)
    ship = ship_for(buy)
    return target, buy, ship, round(target * (1 - CM_FEE) - ship - buy, 2)


def median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def main():
    today = os.environ.get("TODAY") or dt.date.today().isoformat()
    fx = get_fx()
    path = os.environ.get("BULK_FILE") or download_bulk()
    cards, snap, by_oracle = parse(path)
    print(f"{len(snap)} Karten eingelesen, Kurs 1 USD = {fx} EUR")

    guide = cm_guide()
    hist = load(f"{STORE}/history.json.gz", {})
    hist.pop(today, None)
    d1, base1 = pick_base(hist, today, 1)
    d7, base7 = pick_base(hist, today, 7)
    empty = [None] * 5

    # YouTube-Erwähnungen (ein Fehler hier darf das restliche Tool nie stoppen)
    try:
        yt, yt_info = youtube({c["n"] for c in cards.values()}, today)
    except Exception as ex:
        print("YouTube übersprungen:", ex)
        yt, yt_info = {}, {"ok": [], "bad": YT_CHANNELS, "videos": 0}

    def front(n):
        return (n or "").split(" // ")[0].strip()

    def make(cid, foil, base):
        s, b, w = snap[cid], base.get(cid) or empty, base7.get(cid) or empty
        ui, ei = (1, 3) if foil else (0, 2)
        new, old = s[ui], b[ui]
        eur_now, eur_old = s[ei], b[ei]
        rank_now, rank_old = s[4], w[4]
        att = pct(rank_old, rank_now) if rank_now and rank_old else None  # >0 = Aufmerksamkeit steigt
        eu = pct(eur_now, eur_old)
        ytn = len(yt.get(front(cards[cid]["n"]), []))
        # EDHREC-Trend zählt nicht mehr für die Ampel (Auswertung Okt. 2026: schwaches Signal)
        if ytn:
            light = "g"   # auf YouTube erwähnt = echte Aufmerksamkeit
        elif eu is None:
            light = "y"
        elif eu >= 0.10:
            light = "g"   # Cardmarket zieht mit
        else:
            light = "r"
        sales = cm_sales(guide.get(cards[cid].get("cmid")), foil, bool(s[1]))
        target, buy, ship, profit = estimate(eur_now, eur_old, new, old, fx, sales[2] if sales else None)
        e = dict(cards[cid])
        e.pop("cmid", None)
        ch = pct(new, old)
        e.update({"id": cid, "f": foil, "old": old, "new": new, "ch": ch,
                  "eur": eur_now, "euch": eu, "rk": rank_now, "att": att, "l": light, "p": profit,
                  "tg": target, "bp": buy, "sh": ship})
        if ytn:
            e["yt"] = ytn
        if sales:
            e["cs"] = sales   # CM-Verkäufe Ø [gestern, 7 Tage, 30 Tage]
            if sales[1] and sales[2] and sales[1] >= sales[2] * (1 + CM_FOLLOW):
                e["cz"] = 1   # Cardmarket zieht schon nach
        ref = (sales and (sales[1] or sales[2])) or eur_now
        if new and ref and new * fx >= ref * CM_LAG_FACTOR:
            e["lag"] = round(new * fx / ref, 1)   # CM hinkt stark hinterher (US-Preis ist X-mal so hoch)
        if e["rl"] and light == "r" and (ch or 0) > 0:
            e["bo"] = 1  # Preis steigt ohne Aufmerksamkeit und ohne Cardmarket -> möglicher Buyout
        return e

    def spikes_vs(base):
        out = []
        for cid, s in snap.items():
            b = base.get(cid)
            if not b:
                continue
            for foil, i in ((False, 0), (True, 1)):
                if is_spike(s[i], b[i], cards[cid]["rl"]):
                    out.append(make(cid, foil, base))
        return sorted(out, key=lambda e: -e["ch"])

    # RL-Schnitt: typische Preisänderung aller RL-Karten (ab $10) in ca. 7 Tagen (Median)
    rl_avg = None
    if base7:
        rl_avg = median([(s[0] - base7[cid][0]) / base7[cid][0] for cid, s in snap.items()
                         if cards[cid]["rl"] and cid in base7 and s[0] and base7[cid][0]
                         and base7[cid][0] >= RL_MIN_PRICE_USD])
        rl_avg = round(rl_avg, 3) if rl_avg is not None else None

    def rl_week():
        out = []
        if rl_avg is None:
            return out
        for cid, s in snap.items():
            b = base7.get(cid)
            if not b or not cards[cid]["rl"]:
                continue
            for foil, i in ((False, 0), (True, 1)):
                new, old = s[i], b[i]
                if new and old and new >= RL_MIN_PRICE_USD and (new - old) / old - rl_avg >= RL_WEEK_EXTRA:
                    e = make(cid, foil, base7)
                    e["vs"] = round(e["ch"] - rl_avg, 3)
                    out.append(e)
        return out

    today_spikes = spikes_vs(base1) if base1 else []
    week = []
    if base7:
        week = [e for e in spikes_vs(base7) if not e["rl"]][:300] + rl_week()
        week.sort(key=lambda e: -e["ch"])

    # Kandidaten: US steigt, EU hinkt hinterher, Gewinn >= MIN_PROFIT_EUR, Ampel nicht rot
    # (haltende Spikes kommen weiter unten dazu, sobald der Spike-Verlauf geladen ist)
    cand, seen = [], set()
    for e in today_spikes + week:
        k = (e["id"], e["f"])
        if k in seen or e["l"] == "r" or e["p"] is None or e["p"] < MIN_PROFIT_EUR:
            continue
        if e["euch"] is not None and e["ch"] and e["euch"] > e["ch"] / 2:
            continue  # Cardmarket ist schon nachgezogen
        seen.add(k)
        e["k"] = 1  # war Kandidat (für das Ergebnis-Archiv)
        cand.append(e)
    cand.sort(key=lambda e: -e["p"])

    # Spike-Verlauf (90 Tage gespeichert, 30 Tage angezeigt)
    log = load(f"{STORE}/spikelog.json", [])
    log = [e for e in log if e.get("d") != today]
    for e in today_spikes:
        log.append(dict(e, d=today))
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=SPIKELOG_DAYS)).isoformat()
    log = [e for e in log if e["d"] >= cutoff]
    # RL-Spikes: hält der Preis am Folgetag? (1 = bestätigt, 0 = nicht gehalten, fehlt = unbestätigt)
    for e in log:
        if e.get("rl") and e["d"] < today and "ok" not in e and e["id"] in snap:
            cur = snap[e["id"]][1 if e["f"] else 0]
            if cur and e.get("new"):
                e["ok"] = 1 if cur >= e["new"] * RL_HOLD else 0
    log.sort(key=lambda e: (e["d"], e["ch"] or 0), reverse=True)

    # Spikes, die halten: US-Preis seit dem Spike-Tag jeden Tag bei mind. 80 % des Spike-Preises
    # -> Ampel von Rot auf Gelb, und sie dürfen in die Kandidaten (Fall Leech Bonder)
    tday = dt.date.fromisoformat(today)
    held = {}
    for e in log:
        age = (tday - dt.date.fromisoformat(e["d"])).days
        if age < HOLD_DAYS or age > HOLD_LOOKBACK or e["id"] not in snap or not e.get("new"):
            continue
        i = 1 if e["f"] else 0
        after = [h[e["id"]][i] for d, h in sorted(hist.items()) if d > e["d"] and e["id"] in h]
        after.append(snap[e["id"]][i])
        if len(after) >= HOLD_DAYS and all((p or 0) >= e["new"] * HOLD_MIN for p in after):
            e["h"] = age
            if e["l"] == "r":
                e["l"], e["l0"] = "y", "r"
            k = (e["id"], e["f"])
            if k not in held or held[k]["d"] < e["d"]:
                held[k] = e
        elif "h" in e:   # hält nicht mehr -> alte Ampel zurück
            e.pop("h")
            if e.get("l0"):
                e["l"] = e.pop("l0")
    for e in today_spikes + week:
        le = held.get((e["id"], e["f"]))
        if le:
            e["h"] = le["h"]
            if e["l"] == "r":
                e["l"] = "y"
    for k, le in held.items():
        if k in seen:
            continue
        cid, foil = k
        b = [None] * 5
        b[1 if foil else 0], b[3 if foil else 2] = le["old"], le.get("eur")
        e = make(cid, foil, {cid: b})
        e.pop("bo", None)
        e.update({"l": "y" if e["l"] == "r" else e["l"], "h": le["h"], "sd": le["d"]})
        if e["p"] is None or e["p"] < MIN_PROFIT_EUR:
            continue
        if e["euch"] is not None and e["ch"] and e["euch"] > e["ch"] / 2:
            continue  # Cardmarket ist schon nachgezogen
        seen.add(k)
        e["k"] = le["k"] = 1
        cand.append(e)
    cand.sort(key=lambda e: -e["p"])

    # Ergebnis-Archiv: Preis 7/14/30 Tage nach dem Spike festhalten ("x7" = [US, CM-Trend, CM-Verkäufe 7 Tage Ø])
    for e in log:
        if e["id"] in cards:   # Erscheinungsdatum auch bei älteren Einträgen nachtragen
            e["rd"] = cards[e["id"]].get("rd")
        age = (tday - dt.date.fromisoformat(e["d"])).days
        s = snap.get(e["id"])
        if not s:
            continue
        for n in OUTCOME_DAYS:
            if age >= n and f"x{n}" not in e:
                cs = cm_sales(guide.get((cards.get(e["id"]) or {}).get("cmid")), e["f"], bool(s[1]))
                e[f"x{n}"] = [s[1 if e["f"] else 0], s[3 if e["f"] else 2], cs[1] if cs else None]

    def result(e, n):
        """Was wäre nach n Tagen netto geblieben? (Kauf am Spike-Tag zu Trend + Aufschlag,
        Verkauf zum Ø der CM-Verkäufe der letzten 7 Tage – fehlt der, zum dann aktuellen Trend)"""
        x = e.get(f"x{n}")
        if not x or not x[1] or not e.get("eur"):
            return None, None
        buy = e.get("bp") or round(e["eur"] * (1 + BUY_MARKUP), 2)
        ship = e.get("sh") or ship_for(buy)
        sell = (x[2] if len(x) > 2 else None) or x[1]
        return round(x[1] / e["eur"] - 1, 3), round(sell * (1 - CM_FEE) - ship - buy, 2)

    res = {}
    for n in OUTCOME_DAYS:
        all_r = [result(e, n) for e in log]
        all_r = [r for r in all_r if r[0] is not None]
        cand_r = [result(e, n) for e in log if e.get("k")]
        cand_r = [r for r in cand_r if r[0] is not None]
        res[n] = {
            "n": len(all_r),
            "eu": median([r[0] for r in all_r]),
            "eu10": round(sum(r[0] >= 0.10 for r in all_r) / len(all_r), 3) if all_r else None,
            "kn": len(cand_r),
            "kp": median([r[1] for r in cand_r]),
            "kwin": sum(r[1] >= MIN_PROFIT_EUR for r in cand_r),
        }

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

    # YouTube-Liste: Karten, die in den letzten Tagen in Videos vorkamen
    by_name = {}
    for oid, ids in by_oracle.items():
        by_name.setdefault(front(cards[ids[0]]["n"]), []).extend(ids)
    ytl = []
    for n, vids in yt.items():
        ids = by_name.get(n)
        if not ids:
            continue
        with_eur = [i for i in ids if snap[i][2]]
        rep = min(with_eur, key=lambda i: snap[i][2]) if with_eur else ids[0]
        e = make(rep, False, base7)
        e["ytv"] = vids[:5]
        ytl.append(e)
    ytl.sort(key=lambda e: (-e["yt"], -(e["ch"] or 0)))
    ytl = ytl[:150]

    # ---------------- CM-Spikes: Spikes direkt aus Cardmarket-Daten ----------------
    snaps = dict(hist)
    snaps[today] = snap
    sdates = sorted(snaps)

    def cm_entry(cid, foil, s, b):
        """Eintrag für einen Cardmarket-Spike: s = neuer, b = alter Schnappschuss derselben Karte."""
        ui, ei = (1, 3) if foil else (0, 2)
        c = cards[cid]
        w = base7.get(cid) or empty
        rank_now, rank_old = snap[cid][4] if cid in snap else None, w[4]
        att = pct(rank_old, rank_now) if rank_now and rank_old else None
        uch = pct(s[ui], b[ui])
        ytn = len(yt.get(front(c["n"]), []))
        if uch is not None and uch >= CM_US_CONFIRM:
            light = "g"   # US zieht im gleichen Zeitraum mit
        elif ytn:
            light = "g"   # echte Aufmerksamkeit (YouTube)
        elif uch is None:
            light = "y"   # kein US-Preis zum Vergleich
        elif uch <= CM_US_FLAT:
            light = "r"   # nur Cardmarket steigt, US ruhig, kein Hype
        else:
            light = "y"
        e = {k: c.get(k) for k in ("n", "s", "r", "o", "sf", "rl", "ed", "img", "rd")}
        e.update({"id": cid, "f": foil, "old": b[ei], "new": s[ei], "ch": pct(s[ei], b[ei]),
                  "uo": b[ui], "un": s[ui], "uch": uch, "att": att, "rk": rank_now, "l": light})
        if ytn:
            e["yt"] = ytn
        if c["rl"] and light == "r":
            e["bo"] = 1   # RL-Karte springt nur auf Cardmarket -> möglicher Buyout
        return e

    def cm_spikes(new_snap, old_snap):
        out = []
        for cid, s in new_snap.items():
            b = old_snap.get(cid)
            if not b or cid not in cards:
                continue
            for foil, i in ((False, 2), (True, 3)):
                if is_cm_spike(s[i], b[i], cards[cid]["rl"]):
                    out.append(cm_entry(cid, foil, s, b))
        return sorted(out, key=lambda e: -e["ch"])

    cmlog = load(f"{STORE}/cmlog.json", [])
    cmlog = [e for e in cmlog if e.get("d") != today]
    known = {e["d"] for e in cmlog}
    # Heute + fehlende Tage aus dem gespeicherten Preisverlauf nachtragen (beim ersten Lauf: ganze Woche)
    for i in range(1, len(sdates)):
        d = sdates[i]
        if d in known:
            continue
        for e in cm_spikes(snaps[d], snaps[sdates[i - 1]]):
            e["d"] = d
            cmlog.append(e)
    cm_cut = (tday - dt.timedelta(days=CM_LOG_DAYS)).isoformat()
    cmlog = [e for e in cmlog if e["d"] >= cm_cut]
    # Hält der Spike? Preis am nächsten verfügbaren Tag (1 = gehalten, 0 = eingebrochen)
    for e in cmlog:
        if "ok" in e:
            continue
        nxt = next((d for d in sdates if d > e["d"]), None)
        if nxt:
            cur = (snaps[nxt].get(e["id"]) or empty)[3 if e["f"] else 2]
            if cur and e.get("new"):
                e["ok"] = 1 if cur >= e["new"] * CM_HOLD else 0
    cmlog.sort(key=lambda e: (e["d"], e["ch"] or 0), reverse=True)
    for e in cmlog:   # aktueller Cardmarket-Preis (nur für die Anzeige)
        s = snap.get(e["id"])
        e["nw"] = s[3 if e["f"] else 2] if s else None
        if e["id"] in cards:
            e["cm"] = cards[e["id"]]["cm"]
            e["rd"] = cards[e["id"]].get("rd")
    cm_today = [e for e in cmlog if e["d"] == today]
    cm_week = []
    if base7:
        allw = cm_spikes(snap, base7)
        cm_week = [e for e in allw if not e["rl"]][:CM_WEEK_MAX] + [e for e in allw if e["rl"]]
        for e in cm_week:
            e["cm"] = cards[e["id"]]["cm"]
        cm_week.sort(key=lambda e: -e["ch"])
    print(f"CM-Spikes heute: {len(cm_today)}, 7 Tage: {len(cm_week)}, Verlauf: {len(cmlog)}")

    # Preise für Watchlist
    prices = {}
    for cid, s in snap.items():
        if max(x or 0 for x in s[:4]) < 1:
            continue
        w = base7.get(cid) or empty
        c = cards[cid]
        # [9]/[10] = erwarteter Cardmarket-Preis (Non-Foil/Foil) auf Basis ca. 7 Tage
        prices[cid] = [c["n"], c["s"], s[0], s[1], s[2], s[3], w[0], w[2], c["rl"],
                       estimate(s[2], w[2], s[0], w[0], fx)[0], estimate(s[3], w[3], s[1], w[1], fx)[0]]

    hist[today] = snap
    for d in sorted(hist)[:-HISTORY_DAYS]:
        del hist[d]

    save(f"{STORE}/history.json.gz", hist, gz=True)
    save(f"{STORE}/spikelog.json", log)
    save(f"{STORE}/cmlog.json", cmlog)
    save(f"{SITE}/prices.json", prices)
    save(f"{SITE}/data.json", {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "today": today, "base1": d1, "base7": d7, "days": len(hist), "fx": fx,
        "settings": {"min_usd": MIN_PRICE_USD, "min_change": MIN_CHANGE, "fee": CM_FEE,
                     "ship_low": SHIP_LOW_EUR, "ship_high": SHIP_HIGH_EUR, "ship_limit": SHIP_LIMIT_EUR, "min_profit": MIN_PROFIT_EUR, "markup": BUY_MARKUP,
                     "rl_min_usd": RL_MIN_PRICE_USD, "rl_min_change": RL_MIN_CHANGE,
                     "rl_min_abs": RL_MIN_ABS_USD, "rl_week_extra": RL_WEEK_EXTRA,
                     "cm_min_eur": CM_MIN_EUR, "cm_min_change": CM_MIN_CHANGE, "cm_rl_min_eur": CM_RL_MIN_EUR,
                     "cm_rl_min_change": CM_RL_MIN_CHANGE, "cm_rl_min_abs": CM_RL_MIN_ABS,
                     "cm_us_confirm": CM_US_CONFIRM, "cm_us_flat": CM_US_FLAT,
                     "hold_days": HOLD_DAYS, "hold_min": HOLD_MIN,
                     "cm_lag": CM_LAG_FACTOR, "cm_follow": CM_FOLLOW, "cm_guide": bool(guide), "staple": STAPLE_RANK},
        "cmlog": cmlog, "cmweek": cm_week,
        "rl_avg": rl_avg, "res": res,
        "log": [e for e in log if e["d"] >= (tday - dt.timedelta(days=LOG_SHOW_DAYS)).isoformat()], "week": week, "cand": cand, "hype": hype, "ytl": ytl, "yt_info": yt_info,
    })
    print(f"Spikes heute: {len(today_spikes)}, 7 Tage: {len(week)}, Kandidaten: {len(cand)}, "
          f"Hype: {len(hype)}, YouTube: {len(ytl)}")


if __name__ == "__main__":
    main()
