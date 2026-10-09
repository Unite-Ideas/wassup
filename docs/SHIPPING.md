# Shipping and trade

How goods move around the world: a news desk, and a set of map layers you switch on in the
**SHIPPING AND TRADE** panel on the MAP tab (under TRACKS).

## The Shipping and Trade desk

A desk like the others (`config/desks.yaml`, key `shipping`): container ships and tankers, ports,
canals and straits, air cargo, freight rail, trucking, logistics centres, supply chain jams,
border crossings for freight, customs, tariffs and trade disputes. Its stories come from:

- 15 trade press feeds added to `config/sources.yaml`: gCaptain, Splash247, The Loadstar,
  FreightWaves, Journal of Commerce, Seatrade Maritime, The Maritime Executive, Hellenic Shipping
  News, Air Cargo News, Railway Gazette, RailFreight.com, Transport Topics, Supply Chain Dive,
  PortEconomics and the WTO. (Lloyd's List blocks feed readers.)
- GDELT, which now also keeps articles tagged with maritime, piracy, blockade and trade dispute
  themes.

When the newsroom is on, `wassup newsroom setup` hires a desk agent for it like the others.

## The map layers

| Layer | What it shows | Source | How fresh |
| --- | --- | --- | --- |
| Live ships | Cargo ships (light blue) and tankers (orange), pointing where they are heading; dots are ships standing still | [AISStream](https://aisstream.io), needs a free key | live |
| Cargo planes | Planes of cargo airlines in the air (FedEx, UPS, DHL, Atlas, Cargolux and 35 more) | [OpenSky Network](https://opensky-network.org) | every 15 minutes, or 2 with a free account |
| Disruptions | Port closures, dock strikes, attacks on ships, blocked straits, closed borders, new tariffs and bans, storms that shut ports | the news (the local model reads shipping stories) and IMF PortWatch alerts | as stories come in |
| Straits and canals | 28 chokepoints with ships through each this week against normal and a year of daily counts | [IMF PortWatch](https://portwatch.imf.org) (satellite AIS) | daily, about a week behind |
| Ports | 2,065 ports, red when calls this week are far below normal, green when far above | IMF PortWatch | daily, about a week behind |
| US border crossings | Truck wait times at 85 land crossings with Canada and Mexico | US Customs and Border Protection | every 15 minutes |
| Airports | 3,300 airports with scheduled service | OurAirports | monthly |
| Railways | The world's railways | Natural Earth | fixed |

Click anything for its card: a ship's name, type, destination, speed and its trail over the last
48 hours (with links to MarineTraffic and VesselFinder); a strait's ships this week against normal
and against last year, with a year long trend line; a port's calls, imports and exports against
normal; a border crossing's truck wait; a disruption's summary with its story.

The panel lists the latest disruptions, the straits that changed most this week, ports far off
their normal, and the longest truck waits. Click a name to fly there.

### Trains and trucks

There is no free live feed of freight trains or trucks anywhere in the world, so these show up
as the rail network, US border waits, and disruptions from the news (rail strikes, derailments,
closed borders, trucker blockades).

## Setting up live ships (AISStream)

1. Sign in at https://aisstream.io (a GitHub account works) and open **API Keys**. Create a key.
2. In Ubuntu, open your settings file:
   ```bash
   cd ~/wassup
   nano .env
   ```
3. Add this line, with your key after the `=` (never paste the key into a chat or anywhere else):
   ```
   AISSTREAM_API_KEY=your-key-here
   ```
4. Save with Ctrl+O then Enter, and leave with Ctrl+X.
5. Restart Wassup: `docker compose up -d`

Ships appear within a minute. A ship shows once it has sent its type (every six minutes), so the
map fills up over the first ten minutes. Which ships to keep, and the areas to listen to, are in
`config/shipping.yaml` (`ais:`).

## Cargo planes every 2 minutes (optional)

Without an account OpenSky allows one look at the whole world about every 15 minutes. With a free
account: sign up at https://opensky-network.org, open your account page, create an **API client**,
and add its id and secret to `.env`:

```
OPENSKY_CLIENT_ID=...
OPENSKY_CLIENT_SECRET=...
```

then `docker compose up -d`. The list of cargo airlines is in `config/shipping.yaml` (`planes:`).

## How disruptions are found

Every story on the Shipping desk, and every story on another desk with freight words in its
headline (a tanker hit in the Strait of Hormuz is on the Iran desk), is shown to the local model a
few at a time. It says whether the story reports a disruption, what kind, where, how serious, and
whether it is still going on. Two checks keep it honest:

- The kind must be backed by the headlines: a "tariff" needs a duty, levy, quota or ban in them,
  an "attack on a ship" needs a ship. A cut in fuel tax for truckers is not a tariff.
- A strait or sea named in the headlines wins over the country beside it, so Hormuz attacks sit on
  the Strait of Hormuz, not in the middle of Iran.

A story is read again when it doubles in size, so an event's status follows the news.

## What each source is

- **AIS** (Automatic Identification System): every ship over 300 tonnes broadcasts its position,
  speed, course, name, type and destination by radio. AISStream relays what volunteers' receivers
  hear, so coverage is best near coasts and thin in mid ocean. Ships can switch AIS off ("go
  dark"), which the shadow fleet often does.
- **IMF PortWatch** counts port calls and chokepoint transits from satellite AIS, so it covers the
  open ocean, at the cost of about a week's delay.
- **OpenSky** relays ADS-B signals from aircraft, heard by volunteers' receivers. Cargo planes are
  picked by the airline code at the start of their callsign; passenger airlines also carry freight
  in their holds, which this does not count.
