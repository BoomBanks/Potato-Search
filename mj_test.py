"""Testlauf: MTGJSON 90-Tage-Preise laden, Struktur pruefen, auf Scryfall-IDs zuordnen."""
import csv, gzip, io, json, lzma, os, time
import ijson, requests

UA = {"User-Agent": "MTGSpikeTool/2.0"}
B = "https://mtgjson.com/api/v5/"
rep = {"t0": time.strftime("%F %T"), "backend": ijson.backend, "urls": {}}
os.makedirs("out", exist_ok=True)

for u in ["AllPrices.json.xz", "AllPrices.json.gz", "AllIdentifiers.json.xz", "csv/cardIdentifiers.csv",
          "csv/cardIdentifiers.csv.gz", "csv/cardIdentifiers.csv.xz", "parquet/cardIdentifiers.parquet", "Meta.json"]:
    try:
        r = requests.head(B + u, headers=UA, timeout=30, allow_redirects=True)
        rep["urls"][u] = [r.status_code, r.headers.get("Content-Length")]
    except Exception as ex:
        rep["urls"][u] = str(ex)

# Zuordnung uuid -> scryfallId
t = time.time()
ids = {}
for u in ["csv/cardIdentifiers.csv.gz", "csv/cardIdentifiers.csv"]:
    try:
        r = requests.get(B + u, headers=UA, timeout=300)
        r.raise_for_status()
        data = gzip.decompress(r.content) if u.endswith(".gz") else r.content
        rd = csv.DictReader(io.StringIO(data.decode("utf-8")))
        rep["id_cols"] = rd.fieldnames
        for row in rd:
            if row.get("scryfallId"):
                ids[row["uuid"]] = row["scryfallId"]
        rep["id_src"] = u
        break
    except Exception as ex:
        rep.setdefault("id_err", []).append(f"{u}: {ex}")
rep["ids"] = len(ids)
rep["t_ids"] = round(time.time() - t)

# Preise streamen
t = time.time()
out, samples, n, dates, provs = {}, [], 0, set(), {}
with requests.get(B + "AllPrices.json.xz", headers=UA, stream=True, timeout=900) as r:
    r.raise_for_status()
    r.raw.decode_content = True
    f = lzma.open(r.raw)
    rep["t_dl_start"] = round(time.time() - t)
    for uuid, v in ijson.kvitems(f, "data", use_float=True):
        n += 1
        if len(samples) < 2 and "paper" in v:
            samples.append({uuid: v})
        paper = v.get("paper") or {}
        for p in paper:
            provs[p] = provs.get(p, 0) + 1
        cm = ((paper.get("cardmarket") or {}).get("retail") or {})
        tc = ((paper.get("tcgplayer") or {}).get("retail") or {})
        sid = ids.get(uuid)
        if not sid:
            continue
        row = {}
        for key, src in (("c", cm.get("normal")), ("cf", cm.get("foil")), ("u", tc.get("normal")), ("uf", tc.get("foil"))):
            if src:
                row[key] = src
                dates.update(src.keys())
        if row and max([max(x.values()) for x in row.values()]) >= 4:
            out[sid] = row
rep["t_prices"] = round(time.time() - t)
rep["entries"] = n
rep["kept"] = len(out)
rep["providers"] = provs
rep["dates"] = [min(dates), max(dates), len(dates)] if dates else None
rep["samples"] = json.dumps(samples)[:6000]
with gzip.open("out/mj90.json.gz", "wt") as g:
    json.dump(out, g, separators=(",", ":"))
rep["size_gz"] = os.path.getsize("out/mj90.json.gz")
json.dump(rep, open("out/report.json", "w"), indent=1)
print(json.dumps(rep, indent=1)[:3000])
