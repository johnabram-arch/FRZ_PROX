# builds frz_data.json from the nats aixm data + atc_numbers.csv
#
# run whenever nats put out a new dataset (every 28 days) and upload
# the new frz_data.json to the site
#
# does all the downloading/unzipping/parsing here so the website only
# has to do simple distance maths
#
# the file is in five parts:
#   1. settings
#   2. download    - fetch the zip from nats and pull the xml out
#   3. geometry    - turn aixm shapes into a list of (lon, lat) points
#   4. names+phones- tidy up zone names, find a number for each zone
#   5. main        - loop over the zones and write the json

import csv
import hashlib
import io
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from pyproj import Geod


# ---------------------------------------------------------------------------
# 1. settings
# ---------------------------------------------------------------------------

# paths are relative to this script, not to wherever you ran it from
HERE = os.path.dirname(os.path.abspath(__file__))
ATC_FILE = os.path.join(HERE, "atc_numbers.csv")
OUTPUT_FILE = os.path.join(HERE, "frz_data.json")

DATASET_PAGE_URL = "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/"

# xml namespaces, so findtext("aixm:name") knows what "aixm" means
NS = {
    "aixm": "http://www.aixm.aero/schema/5.1",
    "gml": "http://www.opengis.net/gml/3.2",
}

RADIUS_UNIT_TO_M = {
    "[nmi_i]": 1852.0,
    "km": 1000.0,
    "m": 1.0,
    "[ft_i]": 0.3048,
}

# which airspaces end up on the map.
# aerodrome zones are tagged with a localType; everything else in the
# dataset - nuclear sites, prisons, palaces - is a plain ENR 5.1
# prohibited/restricted/danger area, tagged with aixm:type instead.
# add "D" to include danger areas (military ranges, corridors)
AERODROME_LOCAL_TYPES = ("FRZ", "RPZ")
RESTRICTION_TYPES = ("P", "R")

# how finely to draw curves - one point every 3 degrees
ARC_STEP_DEG = 3

# --- independent airstrips (farm strips, gliding sites, private pads) -------
# these have NO legal flight restriction zone. they are advisory only: a
# heads-up that aircraft may be operating low nearby. they go in their own
# file so the main zone data stays purely legal restrictions
INCLUDE_AIRSTRIPS = True
AIRSTRIP_OUTPUT_FILE = os.path.join(HERE, "airstrip_data.json")

# public domain dataset, regenerated daily
AIRSTRIP_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"

# GB plus the crown dependencies, matching the NATS dataset's coverage
AIRSTRIP_COUNTRIES = ("GB", "GG", "JE", "IM")

# "closed" and the big licensed fields are deliberately left out - the
# licensed ones already have a proper FRZ from NATS
AIRSTRIP_TYPES = ("small_airport", "heliport", "balloonport", "seaplane_base")

# advisory radius the website should warn within, and how far from an
# existing NATS zone a strip has to be before we bother listing it
AIRSTRIP_RADIUS_M = 1000.0
AIRSTRIP_DEDUPE_M = 2500.0

geod = Geod(ellps="WGS84")


# ---------------------------------------------------------------------------
# 2. download
# ---------------------------------------------------------------------------

def find_latest_xml():
    # scrape the datasets page for the newest UAS zip link
    resp = requests.get(DATASET_PAGE_URL, timeout=30)
    resp.raise_for_status()

    matches = re.findall(
        r'href="([^"]*UAS_AREA_1/EG_UAS_FR_DS_AREA1_FULL_\d+_XML\.zip)"', resp.text
    )
    if not matches:
        raise RuntimeError(
            "Could not find a 'UAS Flight Restrictions' XML dataset link on "
            "the NATS digital-datasets page. The page layout may have changed."
        )

    url = matches[0]
    if url.startswith("/"):
        url = "https://nats-uk.ead-it.com" + url
    return url


def get_airac_date_from_url(url):
    # airac date is in the filename, e.g. ..._FULL_20260806_XML.zip
    match = re.search(r"_FULL_(\d{4})(\d{2})(\d{2})_XML", url)
    if not match:
        return "unknown"
    return match.group(1) + "-" + match.group(2) + "-" + match.group(3)


def download_and_extract_xml(zip_url):
    print("downloading dataset: " + zip_url)
    resp = requests.get(zip_url, timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()

        xml_names = [n for n in names if n.lower().endswith(".xml")]
        if not xml_names:
            raise RuntimeError("No .xml file found inside the downloaded zip.")
        xml_bytes = zf.read(xml_names[0])

        # the zip usually ships a checksum - worth confirming the
        # download wasn't truncated
        sha_names = [n for n in names if n.lower().endswith(".sha256")]
        if sha_names:
            expected = zf.read(sha_names[0]).decode("utf-8").split()[0].lstrip("*").lower()
            actual = hashlib.sha256(xml_bytes).hexdigest().lower()
            if expected == actual:
                print("checksum verified OK")
            else:
                print("WARNING: sha256 checksum does not match")

    return xml_bytes


# ---------------------------------------------------------------------------
# 3. geometry
# ---------------------------------------------------------------------------

def arc_points(center_lon, center_lat, radius_m, start_deg, end_deg, step_deg=None):
    # walk round the arc dropping a point every few degrees.
    # a full circle is just an arc from 0 to 360
    if step_deg is None:
        step_deg = ARC_STEP_DEG

    sweep = end_deg - start_deg
    steps = max(1, int(abs(sweep) / step_deg))

    points = []
    for i in range(steps + 1):
        angle = (start_deg + sweep * i / steps) % 360
        lon, lat, _ = geod.fwd(center_lon, center_lat, angle, radius_m)
        points.append((lon, lat))
    return points


def read_pos(text):
    # a single "<lat> <lon>" pair - note aixm writes lat first
    lat, lon = text.split()
    return float(lon), float(lat)


def read_poslist(text):
    # posList crams several pairs into one tag, still lat lon
    # e.g. "51.1 -0.5 51.2 -0.5 51.2 -0.4"
    numbers = text.split()

    points = []
    for i in range(0, len(numbers) - 1, 2):
        lat = float(numbers[i])
        lon = float(numbers[i + 1])
        points.append((lon, lat))
    return points


def read_center_and_radius(segment):
    # shared by the arc and circle segment types
    pos = segment.find("gml:pointProperty/aixm:Point/gml:pos", NS)
    center_lon, center_lat = read_pos(pos.text)

    radius = segment.find("gml:radius", NS)
    radius_m = float(radius.text) * RADIUS_UNIT_TO_M.get(radius.get("uom"), 1.0)

    return center_lon, center_lat, radius_m


def read_segment(segment):
    # one piece of a boundary -> a list of (lon, lat) points
    tag = segment.tag.split("}")[-1]

    if tag in ("GeodesicString", "LineStringSegment"):
        # verbose point-by-point form first
        points = []
        for pos in segment.findall("gml:pointProperty/aixm:Point/gml:pos", NS):
            points.append(read_pos(pos.text))
        if points:
            return points

        # nothing found - might be the compact posList form
        poslist = segment.find("gml:posList", NS)
        if poslist is not None:
            return read_poslist(poslist.text)
        return []

    if tag == "ArcByCenterPoint":
        center_lon, center_lat, radius_m = read_center_and_radius(segment)
        start = float(segment.find("gml:startAngle", NS).text)
        end = float(segment.find("gml:endAngle", NS).text)
        return arc_points(center_lon, center_lat, radius_m, start, end)

    if tag == "CircleByCenterPoint":
        center_lon, center_lat, radius_m = read_center_and_radius(segment)
        return arc_points(center_lon, center_lat, radius_m, 0, 360)

    print("  (skipping unrecognised geometry segment type: " + tag + ")")
    return []


# where the outline lives inside an AirspaceTimeSlice
SURFACE_PATH = (
    "aixm:geometryComponent/aixm:AirspaceGeometryComponent"
    "/aixm:theAirspaceVolume/aixm:AirspaceVolume/aixm:horizontalProjection"
    "/aixm:Surface/gml:patches/gml:PolygonPatch/gml:exterior"
)


def build_ring(timeslice):
    # turns the boundary into one flat list of (lon, lat) points.
    # aixm writes this a couple of different ways depending on the zone -
    # circles/arcs go through Curve/segments, rectangles often just use
    # a plain LinearRing instead
    segments = timeslice.find(
        SURFACE_PATH + "/gml:Ring/gml:curveMember/gml:Curve/gml:segments", NS
    )

    if segments is not None:
        ring = []
        for segment in segments:
            ring.extend(read_segment(segment))
        if ring:
            return ring
        # curve existed but was empty - fall through to linearring

    linear_ring = timeslice.find(SURFACE_PATH + "/gml:LinearRing/gml:posList", NS)
    if linear_ring is not None:
        return read_poslist(linear_ring.text)

    return None


# ---------------------------------------------------------------------------
# 4. names and phone numbers
# ---------------------------------------------------------------------------

def tidy_name(text):
    # upper case, punctuation to spaces, no double spaces.
    # gives both sides of a name comparison the same shape
    swapped = ""
    for char in text.upper():
        if char.isalpha() or char.isdigit():
            swapped = swapped + char
        else:
            swapped = swapped + " "
    return " ".join(swapped.split())


def get_place_name(name):
    # "EGCC MANCHESTER RWY 05L" -> "MANCHESTER"
    words = name.split()
    if len(words) < 2:
        return name

    rest = words[1:]
    if "RWY" in rest:
        rest = rest[:rest.index("RWY")]
    return " ".join(rest)


def strip_code_suffix(code):
    # trailing letters off the designator, e.g. "EGCC2FRZ" -> "EGCC2"
    while code and code[-1].isalpha():
        code = code[:-1]
    return code


def get_display_name(name):
    words = name.split()
    if len(words) < 2:
        return name
    return strip_code_suffix(words[0]) + " " + get_place_name(name)


def zone_name(kind, designator, raw_name):
    full = (designator + " " + raw_name).strip()

    # aerodrome zones get the tidy-up (drop the trailing RWY bit etc).
    # restricted areas are already short - "EGR154 OLDBURY" - so leave
    # them exactly as NATS wrote them
    if kind in AERODROME_LOCAL_TYPES:
        return get_display_name(full)
    return full


def load_atc_numbers(path):
    # csv from ATC_NUMBERS.py -> list of (name, phone, description)
    entries = []

    try:
        file = open(path, encoding="utf-8")
    except OSError:
        print("warning: could not find " + path + " - run ATC_NUMBERS.py first")
        return entries

    with file:
        rows = csv.reader(file)
        next(rows, None)  # header

        for row in rows:
            if len(row) < 5:
                continue

            # some numbers have a line break stuck inside them
            phone = row[2].strip().replace("\n", " / ").replace("\r", "")
            if phone == "" or phone == "no number":
                continue

            description = row[4].strip().replace("\n", " ").replace("\r", "")
            entries.append((tidy_name(row[0]), phone, description))

    return entries


def pull_phone_number(notes_text):
    # the aixm notes read like "... Tel: 01234 567890. ..."
    tel_index = notes_text.find("Tel:")
    if tel_index == -1:
        return "No number available."

    after_tel = notes_text[tel_index + 4:]

    # stop at whichever comes first - full stop, newline, or the end
    end = len(after_tel)
    for stop_char in (".", "\n"):
        found = after_tel.find(stop_char)
        if found != -1 and found < end:
            end = found

    return after_tel[:end].strip()


def squash(text):
    # tidy_name with the spaces taken out too, so "TERNHILL" and
    # "TERN HILL" compare equal
    return tidy_name(text).replace(" ", "")


def look_up_phone(zone_name_text, atc_list, allow_partial=True):
    # drop the designator code so we're only comparing place names
    wanted = squash(get_place_name(zone_name_text))
    if wanted == "":
        return ""

    exact = []
    partial = []

    for atc_name, phone, description in atc_list:
        combined = phone
        if description != "":
            combined = phone + " (" + description + ")"

        squashed = squash(atc_name)

        if squashed == wanted:
            if combined not in exact:
                exact.append(combined)
        # short names match far too much, so only use longer ones loosely
        elif allow_partial and len(squashed) >= 5 and (squashed in wanted or wanted in squashed):
            if combined not in partial:
                partial.append(combined)

    return " / ".join(exact or partial)


def classify(local_type, airspace_type, designator):
    # returns "FRZ", "RPZ", "P", "R", "D", or None if we don't want it
    if local_type in AERODROME_LOCAL_TYPES:
        return local_type

    if airspace_type in RESTRICTION_TYPES:
        return airspace_type

    # belt and braces: if the type tag is missing or unrecognised, fall
    # back on the designator - EG R154, EG P611, EG D138, EG RU006,
    # EG R2U006. the rule is: EG, then P/R/D, then something containing
    # at least one digit. requiring a digit stops a four-letter aerodrome
    # code like EGPH or EGDM being read as a restricted area
    code = tidy_name(designator).replace(" ", "")

    if len(code) > 3 and code[:2] == "EG" and code[2] in ("P", "R", "D"):
        rest = code[3:]
        if any(char.isdigit() for char in rest) and code[2] in RESTRICTION_TYPES:
            return code[2]

    return None


def field(timeslice, tag):
    # read one aixm tag as trimmed text, "" if it isn't there
    return (timeslice.findtext("aixm:" + tag, default="", namespaces=NS) or "").strip()


def get_notes_text(timeslice):
    notes = timeslice.findall(
        ".//aixm:Note/aixm:translatedNote/aixm:LinguisticNote/aixm:note", NS
    )
    return " ".join((note.text or "") for note in notes)


# ---------------------------------------------------------------------------
# 5. independent airstrips (advisory only - no legal restriction)
# ---------------------------------------------------------------------------

def ring_centre(ring):
    # average of the points. good enough to ask "is this strip basically
    # the same place as that zone?"
    lon = sum(point[0] for point in ring) / len(ring)
    lat = sum(point[1] for point in ring) / len(ring)
    return lon, lat


def metres_between(lon1, lat1, lon2, lat2):
    _, _, distance = geod.inv(lon1, lat1, lon2, lat2)
    return distance


def download_airstrips():
    print("downloading airstrips: " + AIRSTRIP_URL)
    resp = requests.get(AIRSTRIP_URL, timeout=120)
    resp.raise_for_status()

    rows = csv.DictReader(io.StringIO(resp.text))

    strips = []
    for row in rows:
        if row["iso_country"] not in AIRSTRIP_COUNTRIES:
            continue
        if row["type"] not in AIRSTRIP_TYPES:
            continue

        try:
            lon = float(row["longitude_deg"])
            lat = float(row["latitude_deg"])
        except (ValueError, KeyError):
            continue

        name = row["name"].strip()
        if name == "":
            continue

        # the crowd-sourced data has a few housekeeping entries left in
        if "duplicate" in name.lower() or "delete" in name.lower():
            continue

        strips.append((name, lon, lat))

    return strips


def build_airstrip_list(existing_zones):
    # a plain point per strip, skipping anywhere already covered by a
    # real NATS zone. no rings - the site just measures distance to the
    # point and warns if it's inside AIRSTRIP_RADIUS_M
    strips = download_airstrips()
    print("  " + str(len(strips)) + " strips/pads in the raw data")

    centres = [ring_centre(zone["ring"]) for zone in existing_zones]

    listed = []
    skipped = 0

    for name, lon, lat in strips:
        too_close = False
        for centre_lon, centre_lat in centres:
            # cheap check first - if it's more than about 5 km away in
            # plain degrees it can't be within the dedupe radius, and we
            # skip the slower geodesic maths. 0.05 deg of latitude is
            # roughly 5.5 km anywhere; longitude degrees shrink as you go
            # north, so this is generous rather than tight
            if abs(lat - centre_lat) > 0.05 or abs(lon - centre_lon) > 0.09:
                continue

            if metres_between(lon, lat, centre_lon, centre_lat) < AIRSTRIP_DEDUPE_M:
                too_close = True
                break

        if too_close:
            skipped = skipped + 1
            continue

        listed.append({
            "name": name,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
        })

    listed.sort(key=lambda strip: strip["name"])

    print("  " + str(skipped) + " skipped (already covered by a NATS zone)")
    print("  " + str(len(listed)) + " advisory strips listed")
    return listed


# ---------------------------------------------------------------------------
# 6. main
# ---------------------------------------------------------------------------

print("loading phone numbers from " + ATC_FILE + " ...")
atc_list = load_atc_numbers(ATC_FILE)
print("loaded " + str(len(atc_list)) + " phone entries")
print("")

# when ATC_NUMBERS_1_2.py last actually wrote this file - a separate
# thing from when THIS script ran, since the two run independently
if os.path.exists(ATC_FILE):
    atc_numbers_generated_at = datetime.fromtimestamp(os.path.getmtime(ATC_FILE)).isoformat(timespec="seconds")
else:
    atc_numbers_generated_at = None

zip_url = find_latest_xml()
airac_date = get_airac_date_from_url(zip_url)
xml_bytes = download_and_extract_xml(zip_url)

print("")
print("reading the xml ...")
root = ET.fromstring(xml_bytes)

zones = []
count_from_aixm = 0
count_from_csv = 0
count_no_phone = 0
count_no_shape = 0

kind_counts = {}
skipped_kinds = {}
no_shape_names = []

for airspace in root.findall(".//aixm:Airspace", NS):
    timeslice = airspace.find("aixm:timeSlice/aixm:AirspaceTimeSlice", NS)
    if timeslice is None:
        continue

    local_type = field(timeslice, "localType")
    airspace_type = field(timeslice, "type")
    designator = field(timeslice, "designator")

    # is this one of ours?
    kind = classify(local_type, airspace_type, designator)
    if kind is None:
        key = (airspace_type, local_type)
        skipped_kinds[key] = skipped_kinds.get(key, 0) + 1
        continue

    name = zone_name(kind, designator, field(timeslice, "name"))

    # phone: try the aixm note, then the csv of ATC units
    phone = pull_phone_number(get_notes_text(timeslice))
    phone_source = "AIXM note"

    if phone != "No number available.":
        count_from_aixm = count_from_aixm + 1
    else:
        # aerodrome zones can match loosely ("MANCHESTER" vs "MANCHESTER
        # BARTON"). restricted areas must match the place name exactly,
        # otherwise somewhere like BERKELEY picks up an unrelated tower
        fallback = look_up_phone(
            name, atc_list, allow_partial=(kind in AERODROME_LOCAL_TYPES)
        )

        if fallback != "":
            phone = fallback
            phone_source = "atcadvisor.com"
            count_from_csv = count_from_csv + 1
        else:
            phone_source = "none"
            count_no_phone = count_no_phone + 1

    # shape
    ring = build_ring(timeslice)
    if not ring:
        count_no_shape = count_no_shape + 1
        no_shape_names.append(name + " [" + kind + "]")
        continue

    # 5 decimal places is about 1m - plenty accurate, keeps the file small
    tidy_ring = [[round(lon, 5), round(lat, 5)] for lon, lat in ring]

    kind_counts[kind] = kind_counts.get(kind, 0) + 1

    zones.append({
        "name": name,
        "kind": kind,
        "phone": phone,
        "phone_source": phone_source,
        "ring": tidy_ring,
    })

print("")
print("building " + OUTPUT_FILE + " ...")

# one timestamp for this whole run - used in both output files below, so
# they always agree on when this refresh actually happened. this is
# separate from airac_date, which is NATS' own cycle date, not ours.
run_timestamp = datetime.now().isoformat(timespec="seconds")

data = {
    "generated_at": run_timestamp,
    "atc_numbers_generated_at": atc_numbers_generated_at,
    "airac_date": airac_date,
    "source_url": zip_url,
    "zone_count": len(zones),
    "zones": zones,
}

output_file = open(OUTPUT_FILE, "w", encoding="utf-8")
json.dump(data, output_file)
output_file.close()

# --- second, separate file: advisory airstrips ----------------------------
strip_count = 0
if INCLUDE_AIRSTRIPS:
    print("")
    print("building " + AIRSTRIP_OUTPUT_FILE + " ...")
    strips = build_airstrip_list(zones)
    strip_count = len(strips)

    strip_data = {
        "generated_at": run_timestamp,
        "note": "ADVISORY ONLY - these are unlicensed strips with no legal "
                "flight restriction zone. Nothing here is a no-fly area.",
        "source": "OurAirports (public domain)",
        "source_url": AIRSTRIP_URL,
        "advisory_radius_m": AIRSTRIP_RADIUS_M,
        "strip_count": strip_count,
        "strips": strips,
    }

    strip_file = open(AIRSTRIP_OUTPUT_FILE, "w", encoding="utf-8")
    json.dump(strip_data, strip_file)
    strip_file.close()

print("")
print("done")
print("airac date:                " + airac_date)
print("zones written:             " + str(len(zones)))
print("phone from AIXM note:      " + str(count_from_aixm))
print("phone from atcadvisor csv: " + str(count_from_csv))
print("no phone number at all:    " + str(count_no_phone))
print("skipped (no shape):        " + str(count_no_shape))
if INCLUDE_AIRSTRIPS:
    print("advisory airstrips:        " + str(strip_count) + " (separate file)")

print("")
print("by kind:")
for kind in sorted(kind_counts):
    print("  " + kind + ": " + str(kind_counts[kind]))

if no_shape_names:
    print("")
    print("had no usable shape (probably defined by reference to another airspace):")
    for entry in no_shape_names:
        print("  " + entry)

if skipped_kinds:
    print("")
    print("airspaces in the file we did NOT keep (type, localType):")
    for key in sorted(skipped_kinds, key=lambda k: -skipped_kinds[k]):
        label = "type=" + (key[0] or "-") + " localType=" + (key[1] or "-")
        print("  " + label + ": " + str(skipped_kinds[key]))
    print("  ^ check this list - if something here should be on the map,")
    print("    add its type to RESTRICTION_TYPES or AERODROME_LOCAL_TYPES")
