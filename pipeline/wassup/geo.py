"""Gazetteer: turns place names in text into coordinates, and maps GDELT locations onto
the same place keys so the globe shows one node per city."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .text import PhraseMatcher

DATA = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Place:
    key: str
    name: str
    country: str | None
    kind: str  # country | region | city
    lat: float
    lon: float


# Extra names and demonyms that point at a country. Proper case: matching is case sensitive,
# which keeps "turkey" the bird and "chad" the name out.
COUNTRY_ALIASES = {
    "US": ["US", "U.S.", "USA", "United States", "America", "American", "Americans"],
    "GB": ["UK", "U.K.", "Britain", "British", "England", "Scotland", "Wales"],
    "RU": ["Russia", "Russian", "Russians", "Kremlin", "Россия"],
    "UA": ["Ukraine", "Ukrainian", "Ukrainians", "Україна", "Украина"],
    "BY": ["Belarus", "Belarusian"],
    "IR": ["Iran", "Iranian", "Iranians", "ایران", "إيران"],
    "IL": ["Israel", "Israeli", "Israelis"],
    "PS": ["Palestine", "Palestinian", "Palestinians", "Gaza Strip", "West Bank"],
    "LB": ["Lebanon", "Lebanese"],
    "SY": ["Syria", "Syrian", "Syrians"],
    "IQ": ["Iraq", "Iraqi", "Iraqis"],
    "YE": ["Yemen", "Yemeni", "Houthi", "Houthis"],
    "SA": ["Saudi Arabia", "Saudi", "Saudis"],
    "AE": ["UAE", "United Arab Emirates", "Emirati"],
    "QA": ["Qatar", "Qatari"],
    "TR": ["Turkey", "Türkiye", "Turkish"],
    "EG": ["Egypt", "Egyptian"],
    "JO": ["Jordanian"],
    "CN": ["China", "Chinese", "Beijing's"],
    "TW": ["Taiwan", "Taiwanese"],
    "KP": ["North Korea", "North Korean", "Pyongyang's"],
    "KR": ["South Korea", "South Korean"],
    "JP": ["Japan", "Japanese"],
    "IN": ["India", "Indian"],
    "PK": ["Pakistan", "Pakistani"],
    "AF": ["Afghanistan", "Afghan", "Taliban"],
    "DE": ["Germany", "German"],
    "FR": ["France", "French"],
    "IT": ["Italy", "Italian"],
    "ES": ["Spain", "Spanish"],
    "PL": ["Poland", "Polish"],
    "HU": ["Hungary", "Hungarian"],
    "RS": ["Serbia", "Serbian"],
    "XK": ["Kosovo"],
    "AM": ["Armenia", "Armenian"],
    "AZ": ["Azerbaijan", "Azerbaijani"],
    "MX": ["Mexico", "Mexican"],
    "CU": ["Cuba", "Cuban"],
    "VE": ["Venezuela", "Venezuelan"],
    "CO": ["Colombia", "Colombian"],
    "BR": ["Brazil", "Brazilian"],
    "AR": ["Argentina", "Argentine"],
    "HT": ["Haiti", "Haitian"],
    "CA": ["Canada", "Canadian", "Ottawa's"],
    "AU": ["Australia", "Australian"],
    "SD": ["Sudan", "Sudanese"],
    "SS": ["South Sudan"],
    "CD": ["DR Congo", "DRC", "Congolese"],
    "NG": ["Nigeria", "Nigerian"],
    "ET": ["Ethiopia", "Ethiopian"],
    "SO": ["Somalia", "Somali"],
    "LY": ["Libya", "Libyan"],
    "ML": ["Mali", "Malian"],
    "NE": ["Niger"],
    "BF": ["Burkina Faso"],
    "MM": ["Myanmar", "Burma", "Burmese"],
    "PH": ["Philippines", "Filipino", "Philippine"],
    "VN": ["Vietnam", "Vietnamese"],
    "MD": ["Moldova", "Moldovan"],
    "FI": ["Finland", "Finnish"],
    "SE": ["Sweden", "Swedish"],
    "NO": ["Norway", "Norwegian"],
    "EE": ["Estonia", "Estonian"],
    "LV": ["Latvia", "Latvian"],
    "LT": ["Lithuania", "Lithuanian"],
    "RO": ["Romania", "Romanian"],
    "GR": ["Greece", "Greek"],
    "VA": ["Vatican", "Holy See"],
}

# Landmarks that stand in for a capital city.
LANDMARKS = {
    "White House": "US", "Capitol Hill": "US", "Pentagon": "US", "Oval Office": "US",
    "Downing Street": "GB", "Élysée": "FR", "Kremlin": "RU", "Knesset": "IL",
}

# Country names that are too ambiguous to match on their own.
SKIP_COUNTRY_NAMES = {"Georgia", "Jordan", "Chad", "Jersey", "Guernsey", "Man", "Niger"}
# City names that are common words or first names.
SKIP_CITY_NAMES = {
    "Victoria", "Male", "Sale", "Hamilton", "Charlotte", "Aurora", "Mesa", "Garland", "Irving", "Plano",
    "Nice", "Split", "Douglas", "Leon", "Colon", "Mobile", "Independence", "Orange", "Santa Cruz",
    "Cordoba", "Of", "Bat", "Merida", "Valencia", "Kingston", "Georgetown", "Concepcion", "Trinidad",
    "Laval", "Surrey", "Reading", "Bristol", "Phoenix", "Lincoln", "Jackson", "Columbus", "Madison",
    "Austin", "Gary", "Florence", "Paterson", "Dallas", "Houston", "Denver", "Raleigh", "Durham",
    "Brest", "Bath", "Derby", "Wellington", "Richmond", "Mandalay", "Salem", "Nancy", "Tours",
    "Troy", "Athens", "Alexandria", "Lagos", "Panama", "Mexico", "Kuwait", "Djibouti", "Singapore",
    "Monaco", "Luxembourg", "Vatican City", "Guatemala", "San Marino", "Andorra", "Brazzaville",
}


def _f(v: str) -> float | None:
    return float(v) if v else None


class Gazetteer:
    def __init__(self, data_dir: Path = DATA):
        self.countries: dict[str, Place] = {}
        self.fips_to_iso: dict[str, str] = {}
        self.cities: list[Place] = []
        names: dict[str, Place] = {}

        # Country markers go at the middle of the country (largest landmass), not on the
        # capital, so "Russia" and "Moscow" do not sit on top of each other.
        centers: dict[str, tuple[float, float]] = {}
        if (data_dir / "country_centroids.tsv").exists():
            with open(data_dir / "country_centroids.tsv", encoding="utf-8") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    centers[row["iso2"]] = (float(row["lat"]), float(row["lon"]))

        with open(data_dir / "countries.tsv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                if row["fips"]:
                    self.fips_to_iso[row["fips"]] = row["iso2"]
                lat, lon = centers.get(row["iso2"], (_f(row["lat"]), _f(row["lon"])))
                if lat is None:
                    continue
                p = Place(f"cc:{row['iso2']}", row["name"], row["iso2"], "country", lat, lon)
                self.countries[row["iso2"]] = p
                if row["name"] not in SKIP_COUNTRY_NAMES:
                    names.setdefault(row["name"], p)

        for iso, aliases in COUNTRY_ALIASES.items():
            if iso in self.countries:
                for a in aliases:
                    names[a] = self.countries[iso]
                    # Plural demonyms: Canadians, Iranians, Israelis, Afghans.
                    if a[:1].isupper() and " " not in a and a.endswith(("an", "i")):
                        names.setdefault(a + "s", self.countries[iso])

        capitals: dict[str, Place] = {}
        with open(data_dir / "cities.tsv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                p = Place(f"gn:{row['geonameid']}", row["name"], row["country"], "city", float(row["lat"]), float(row["lon"]))
                self.cities.append(p)
                if row["capital"] == "1":
                    capitals.setdefault(row["country"], p)
                for n in {row["name"], row["asciiname"]}:
                    if len(n) >= 4 and n not in SKIP_CITY_NAMES and n not in names:
                        names[n] = p  # cities are sorted by population, so the biggest wins
        for landmark, iso in LANDMARKS.items():
            if iso in capitals:
                names[landmark] = capitals[iso]
        # Common alternate spellings.
        for alt, canonical in {"Kiev": "Kyiv", "Odessa": "Odesa", "Kharkov": "Kharkiv", "Washington, D.C.": "Washington"}.items():
            if canonical in names:
                names[alt] = names[canonical]

        self._names = names
        self._matcher = PhraseMatcher(names.keys(), case_sensitive=True)
        self._city_xyz = _to_xyz(np.array([[c.lat, c.lon] for c in self.cities])) if self.cities else None

    def find(self, text: str, limit: int = 4) -> list[Place]:
        """Places mentioned in text, most specific first (cities before countries)."""
        found: dict[str, Place] = {}
        for name in self._matcher.find(text):
            p = self._names[name]
            found.setdefault(p.key, p)
        places = list(found.values())
        # Drop a country when one of its cities is already present.
        city_countries = {p.country for p in places if p.kind == "city"}
        places = [p for p in places if not (p.kind == "country" and p.country in city_countries)]
        places.sort(key=lambda p: 0 if p.kind == "city" else 1)
        return places[:limit]

    def by_name(self, name: str) -> Place | None:
        return self._names.get(name)

    def country(self, iso2: str) -> Place | None:
        return self.countries.get(iso2)

    def snap_city(self, lat: float, lon: float, country: str | None, max_km: float = 30) -> Place | None:
        """Nearest known city within max_km, so GDELT points reuse gazetteer place keys."""
        if self._city_xyz is None:
            return None
        d = np.linalg.norm(self._city_xyz - _to_xyz(np.array([[lat, lon]])), axis=1) * 6371
        i = int(np.argmin(d))
        c = self.cities[i]
        if d[i] <= max_km and (country is None or c.country == country):
            return c
        return None


def _to_xyz(latlon: np.ndarray) -> np.ndarray:
    lat, lon = np.radians(latlon[:, 0]), np.radians(latlon[:, 1])
    return np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=1)


@lru_cache
def gazetteer() -> Gazetteer:
    return Gazetteer()


# Country code top level domains that differ from the ISO code.
_TLD_ISO = {"uk": "GB"}
# Generic suffixes under which a country code is the second to last label (bbc.co.uk, abc.net.au).
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "gob", "gouv"}


def country_of_domain(domain_or_url: str | None) -> str | None:
    """The country an outlet's web domain points to (cbc.ca -> CA, abc.net.au -> AU), if any."""
    if not domain_or_url:
        return None
    d = domain_or_url.split("//")[-1].split("/")[0].split(":")[0].lower()
    labels = d.split(".")
    tld = labels[-1] if labels else ""
    if len(tld) != 2:
        return None
    iso = _TLD_ISO.get(tld, tld.upper())
    return iso if gazetteer().country(iso) else None
