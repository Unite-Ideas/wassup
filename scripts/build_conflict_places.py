"""Build the place list used to put strikes on the map (pipeline/wassup/tracks/strikes.py).

The main gazetteer only has big cities. Strike reports name villages, often in Ukrainian,
Russian, Arabic or Hebrew, so this keeps every populated place in Ukraine, the Russian border
regions, Israel, Palestine, Lebanon and Syria, and every place of 500 people or more in the rest
of Russia, Belarus, Moldova, Iran, Iraq, Jordan and Yemen, from GeoNames, with their names in
those scripts too.

Input:  a folder with these files from https://download.geonames.org/export/dump/
        UA.zip RU.zip IL.zip PS.zip LB.zip SY.zip cities500.zip admin1CodesASCII.txt
Output: pipeline/wassup/data/conflict_places.tsv.gz
        geonameid, name, names (other names, | separated), country, admin1, population, lat, lon

Usage:  python scripts/build_conflict_places.py /path/to/folder
"""
import csv
import gzip
import io
import re
import sys
import zipfile
from pathlib import Path

FULL = ["UA", "IL", "PS", "LB", "SY"]          # every village
RU_BORDER = {"09", "10", "41", "61", "86", "38"}  # Belgorod, Bryansk, Kursk, Rostov, Voronezh, Krasnodar
TOWNS = {"RU", "BY", "MD", "IR", "IQ", "JO", "YE"}  # 500 people or more
SKIP_CODES = {"PPLH", "PPLX"}                    # historical places, parts of a city
# Latin, Cyrillic, Hebrew and Arabic letters only: the scripts strike reports are written in.
SCRIPTS = re.compile(r"^[\sA-Za-z\u00C0-\u024F\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF'\u2019`.\-()]+$")
OUT = Path(__file__).resolve().parent.parent / "pipeline" / "wassup" / "data" / "conflict_places.tsv.gz"


def _rows(fh):
    yield from csv.reader(io.TextIOWrapper(fh, "utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE)


def main(folder: str) -> None:
    d = Path(folder)
    admin1 = {}
    with open(d / "admin1CodesASCII.txt", encoding="utf-8") as fh:
        for r in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            admin1[r[0]] = r[2]
    seen, rows = set(), []

    def add(r):
        gid, name, ascii_name, alt, lat, lon, fclass, fcode, cc = r[:9]
        if fclass != "P" or fcode in SKIP_CODES or gid in seen:
            return
        seen.add(gid)
        names = []
        for n in [ascii_name, *alt.split(",")]:
            n = n.strip()
            if n and n != name and len(n) <= 50 and SCRIPTS.match(n) and n not in names:
                names.append(n)
        rows.append((gid, name, "|".join(names), cc, admin1.get(f"{cc}.{r[10]}", ""), int(r[14] or 0),
                     f"{float(lat):.4f}", f"{float(lon):.4f}"))

    for cc in FULL + ["RU"]:
        with zipfile.ZipFile(d / f"{cc}.zip") as zf, zf.open(f"{cc}.txt") as fh:
            for r in _rows(fh):
                if cc != "RU" or r[10] in RU_BORDER:
                    add(r)
    with zipfile.ZipFile(d / "cities500.zip") as zf, zf.open("cities500.txt") as fh:
        for r in _rows(fh):
            if r[8] in TOWNS:
                add(r)
    rows.sort(key=lambda x: -x[5])
    with gzip.open(OUT, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\")
        w.writerow(["geonameid", "name", "names", "country", "admin1", "population", "lat", "lon"])
        w.writerows(rows)
    print(f"{len(rows)} places -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
