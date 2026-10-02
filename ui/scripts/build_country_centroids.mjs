// Country map centers for the gazetteer: the centroid of each country's largest landmass, so
// country markers sit inside the country instead of on top of its capital.
//   cd ui && node scripts/build_country_centroids.mjs /path/to/countryInfo.txt
import fs from "fs";
import { feature } from "topojson-client";
import { geoArea, geoCentroid } from "d3-geo";

const info = process.argv[2];
const numToIso = {};
for (const line of fs.readFileSync(info, "utf8").split("\n")) {
  if (!line || line.startsWith("#")) continue;
  const f = line.split("\t");
  numToIso[String(Number(f[2]))] = f[0];
}
const topo = JSON.parse(fs.readFileSync(new URL("../node_modules/world-atlas/countries-50m.json", import.meta.url)));
const fc = feature(topo, topo.objects.countries);
const rows = ["iso2\tlat\tlon"];
for (const f of fc.features) {
  const iso = numToIso[String(Number(f.id))];
  if (!iso || !f.geometry) continue;
  const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
  const biggest = polys.map((p) => ({ type: "Polygon", coordinates: p })).sort((a, b) => geoArea(b) - geoArea(a))[0];
  const [lon, lat] = geoCentroid(biggest);
  rows.push(`${iso}\t${lat.toFixed(4)}\t${lon.toFixed(4)}`);
}
fs.writeFileSync(new URL("../../pipeline/wassup/data/country_centroids.tsv", import.meta.url), rows.join("\n") + "\n");
console.log(`${rows.length - 1} country centers written`);
