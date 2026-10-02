"""Build the bundled gazetteer used to geotag sources that do not come with coordinates.

Inputs (from https://download.geonames.org/export/dump/):
  countryInfo.txt, cities15000.zip

Output:
  pipeline/wassup/data/countries.tsv  iso2, iso3, fips, name, capital, lat, lon
  pipeline/wassup/data/cities.tsv     geonameid, name, asciiname, country, population, capital, lat, lon

Usage:
  python scripts/build_gazetteer.py /path/to/countryInfo.txt /path/to/cities15000.zip

Country centers (country_centroids.tsv) come from ui/scripts/build_country_centroids.mjs.
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

MIN_POP = 250_000
OUT = Path(__file__).resolve().parent.parent / "pipeline" / "wassup" / "data"


def main(country_info: str, cities_zip: str) -> None:
    cities = []
    with zipfile.ZipFile(cities_zip) as zf:
        with zf.open("cities15000.txt") as fh:
            for row in csv.reader(io.TextIOWrapper(fh, "utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE):
                gid, name, ascii_name, _alt, lat, lon, _fc, fcode, cc = row[:9]
                pop = int(row[14] or 0)
                is_capital = fcode == "PPLC"
                if pop >= MIN_POP or is_capital:
                    cities.append((gid, name, ascii_name, cc, pop, int(is_capital), lat, lon))

    capitals = {c[3]: c for c in cities if c[5]}
    countries = []
    for line in Path(country_info).read_text("utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = line.split("\t")
        iso2, iso3, fips, name, capital = f[0], f[1], f[3], f[4], f[5]
        cap = capitals.get(iso2)
        lat, lon = (cap[6], cap[7]) if cap else ("", "")
        countries.append((iso2, iso3, fips, name, capital, lat, lon))

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "countries.tsv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["iso2", "iso3", "fips", "name", "capital", "lat", "lon"])
        w.writerows(countries)
    with open(OUT / "cities.tsv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["geonameid", "name", "asciiname", "country", "population", "capital", "lat", "lon"])
        w.writerows(sorted(cities, key=lambda c: -c[4]))
    print(f"{len(countries)} countries, {len(cities)} cities written to {OUT}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
