"""Build the towns list used to place movement tracks (migrant caravans) on the map.

The main gazetteer only has big cities; caravans move through small towns. This keeps every
town of 1,000 people or more in Mexico and Central America (and the US border strip, and the
Darien gap), from GeoNames.

Input:  cities1000.zip from https://download.geonames.org/export/dump/
Output: pipeline/wassup/data/towns.tsv   geonameid, name, asciiname, country, population, lat, lon

Usage:  python scripts/build_towns.py /path/to/cities1000.zip
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

COUNTRIES = {"MX", "GT", "BZ", "HN", "SV", "NI", "CR", "PA"}
BORDER = {"US": (-118.5, 25.5, -96.5, 33.6), "CO": (-79.5, 6.5, -74.0, 10.0)}  # US border strip, Darien
OUT = Path(__file__).resolve().parent.parent / "pipeline" / "wassup" / "data" / "towns.tsv"


def main(zip_path: str) -> None:
    rows = []
    with zipfile.ZipFile(zip_path) as zf, zf.open("cities1000.txt") as fh:
        for r in csv.reader(io.TextIOWrapper(fh, "utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE):
            gid, name, ascii_name, _alt, lat, lon, _fc, fcode, cc = r[:9]
            if fcode == "PPLX":
                continue
            la, lo = float(lat), float(lon)
            if cc in COUNTRIES or (cc in BORDER and BORDER[cc][0] <= lo <= BORDER[cc][2] and BORDER[cc][1] <= la <= BORDER[cc][3]):
                rows.append((gid, name, ascii_name, cc, int(r[14] or 0), f"{la:.4f}", f"{lo:.4f}"))
    rows.sort(key=lambda x: -x[4])
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["geonameid", "name", "asciiname", "country", "population", "lat", "lon"])
        w.writerows(rows)
    print(f"{len(rows)} towns -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
