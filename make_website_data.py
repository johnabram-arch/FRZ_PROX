# turns the nats aixm data + atc_numbers.csv into ONE json file
# that the website can read
#
# run this whenever nats release a new dataset (every 28 days),
# then upload the new frz_data.json to the website
#
# all the hard work (downloading, unzipping, reading the xml, turning
# arcs and circles into lists of points) happens HERE, so the website
# only has to do the simple distance maths

import csv
import hashlib
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

import requests
from pyproj import Geod

DATASET_PAGE_URL = "https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/"
ATC_FILE = "atc_numbers.csv"
OUTPUT_FILE = "frz_data.json"

NS = {
    "aixm": "http://www.aixm.aero/schema/5.1",
    "gml": "http://www.opengis.net/gml/3.2",
    "message": "http://www.aixm.aero/schema/5.1/message",
}

RADIUS_UNIT_TO_M = {
    "[nmi_i]": 1852.0,
    "km": 1000.0,
    "m": 1.0,
    "[ft_i]": 0.3048,
}

geod = Geod(ellps="WGS84")


# ---------------------------------------------------------------------------
# getting the dataset from nats (same as your frz script)
# ---------------------------------------------------------------------------

def find_latest_xml():
    resp = requests.get(DATASET_PAGE_URL, timeout=30)
    resp.raise_for_status()
    html = resp.text

    matches = re.findall(
        r'href="([^"]*UAS_AREA_1/EG_UAS_FR_DS_AREA1_FULL_\d+_XML\.zip)"', html,
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


# the airac date is in the filename, e.g. ..._FULL_20260806_XML.zip
def get_airac_date_from_url(url):
    match = re.search(r"_FULL_(\d{4})(\d{2})(\d{2})_XML", url)
    if match:
        return match.group(1) + "-" + match.group(2) + "-" + match.group(3)
    return "unknown"


def download_and_extract_xml(zip_url):
    print("downloading dataset: " + zip_url)
    resp = requests.get(zip_url, timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise RuntimeError("No .xml file found inside the downloaded zip.")
        xml_bytes = zf.read(xml_names[0])

        sha_names = [n for n in zf.namelist() if n.lower().endswith(".sha256")]
        if sha_names:
            sha_text = zf.read(sha_names[0]).decode("utf-8").strip()
            expected = sha_text.split()[0].lstrip("*").lower()
            actual = hashlib.sha256(xml_bytes).hexdigest().lower()
            if expected != actual:
                print("WARNING: sha256 checksum does not match")
            else:
                print("checksum verified OK")

    return xml_bytes


# ---------------------------------------------------------------------------
# geometry - turning aixm curves into a plain list of points
# ---------------------------------------------------------------------------

def arc_points(center_lon, center_lat, radius_m, start_deg, end_deg, step_deg=3):
    sweep = end_deg - start_deg

    steps = max(1, int(abs(sweep) / step_deg))
    pts = []
    for i in range(steps + 1):
        angle = (start_deg + sweep * i / steps) % 360
        lon, lat, _ = geod.fwd(center_lon, center_lat, angle, radius_m)
        pts.append((lon, lat))
    return pts


def circle_points(center_lon, center_lat, radius_m, step_deg=3):
    steps = int(360 / step_deg)
    pts = []
    for i in range(steps + 1):
        angle = (step_deg * i) % 360
        lon, lat, _ = geod.fwd(center_lon, center_lat, angle, radius_m)
        pts.append((lon, lat))
    return pts


def _pos_to_lonlat(text):
    lat_str, lon_str = text.split()
    return float(lon_str), float(lat_str)


def build_ring(timeslice):
    segments = timeslice.find(
        "aixm:geometryComponent/aixm:AirspaceGeometryComponent"
        "/aixm:theAirspaceVolume/aixm:AirspaceVolume/aixm:horizontalProjection"
        "/aixm:Surface/gml:patches/gml:PolygonPatch/gml:exterior/gml:Ring"
        "/gml:curveMember/gml:Curve/gml:segments",
        NS,
    )
    if segments is None:
        return None

    ring = []
    for seg in segments:
        tag = seg.tag.split("}")[-1]

        if tag in ("GeodesicString", "LineStringSegment"):
            for pos in seg.findall("gml:pointProperty/aixm:Point/gml:pos", NS):
                ring.append(_pos_to_lonlat(pos.text))

        elif tag == "ArcByCenterPoint":
            pos = seg.find("gml:pointProperty/aixm:Point/gml:pos", NS)
            center_lon, center_lat = _pos_to_lonlat(pos.text)
            radius_elem = seg.find("gml:radius", NS)
            radius_m = float(radius_elem.text) * RADIUS_UNIT_TO_M.get(radius_elem.get("uom"), 1.0)
            start_deg = float(seg.find("gml:startAngle", NS).text)
            end_deg = float(seg.find("gml:endAngle", NS).text)
            ring.extend(arc_points(center_lon, center_lat, radius_m, start_deg, end_deg))

        elif tag == "CircleByCenterPoint":
            pos = seg.find("gml:pointProperty/aixm:Point/gml:pos", NS)
            center_lon, center_lat = _pos_to_lonlat(pos.text)
            radius_elem = seg.find("gml:radius", NS)
            radius_m = float(radius_elem.text) * RADIUS_UNIT_TO_M.get(radius_elem.get("uom"), 1.0)
            ring.extend(circle_points(center_lon, center_lat, radius_m))

        else:
            print("  (skipping unrecognised geometry segment type: " + tag + ")")

    return ring


# ---------------------------------------------------------------------------
# names and phone numbers (same logic as your frz script)
# ---------------------------------------------------------------------------

def pull_phone_number(description):
    tel_index = description.find("Tel:")
    if tel_index == -1:
        return "No number available."

    after_tel = description[tel_index + 4:]

    end_index = len(after_tel)

    dot_index = after_tel.find(".")
    if dot_index != -1 and dot_index < end_index:
        end_index = dot_index

    newline_index = after_tel.find("\n")
    if newline_index != -1 and newline_index < end_index:
        end_index = newline_index

    phone = after_tel[:end_index]
    phone = phone.strip()

    return phone


def get_place_name(name):
    words = name.split()

    if len(words) < 2:
        return name

    rest = words[1:]

    if "RWY" in rest:
        rwy_index = rest.index("RWY")
        place_words = rest[:rwy_index]
    else:
        place_words = rest

    return " ".join(place_words)


def strip_code_suffix(code):
    index = len(code)

    while index > 0 and code[index - 1].isalpha():
        index = index - 1

    return code[:index]


def get_display_name(name):
    words = name.split()

    if len(words) < 2:
        return name

    code = strip_code_suffix(words[0])
    place = get_place_name(name)

    return code + " " + place


def tidy_name(name):
    name = name.upper()

    tidy = ""
    i = 0
    while i < len(name):
        char = name[i]

        if char.isalpha() or char.isdigit():
            tidy = tidy + char
        else:
            tidy = tidy + " "

        i = i + 1

    while "  " in tidy:
        tidy = tidy.replace("  ", " ")

    return tidy.strip()


def load_atc_numbers(path):
    entries = []

    try:
        file = open(path, encoding="utf-8")
    except Exception:
        print("warning: could not find " + path + " - run ATC_NUMBERS.py first")
        return entries

    reader = csv.reader(file)

    row_number = 0
    for row in reader:
        row_number = row_number + 1

        if row_number == 1:
            continue

        if len(row) < 5:
            continue

        name = tidy_name(row[0])
        # some numbers have a line break inside them
        phone = row[2].strip().replace("\n", " / ").replace("\r", "")
        description = row[4].strip().replace("\n", " ").replace("\r", "")

        if phone == "" or phone == "no number":
            continue

        entries.append([name, phone, description])

    file.close()
    return entries


def look_up_phone(zone_name, atc_list):
    # drop the leading designator code so we compare place names only
    wanted = tidy_name(get_place_name(zone_name))
    if wanted == "":
        return ""

    found = []

    for entry in atc_list:
        atc_name = entry[0]
        phone = entry[1]
        description = entry[2]

        combined = phone
        if description != "":
            combined = phone + " (" + description + ")"

        if atc_name == wanted:
            if combined not in found:
                found.append(combined)

    if len(found) == 0:
        for entry in atc_list:
            atc_name = entry[0]
            phone = entry[1]
            description = entry[2]

            if len(atc_name) < 5:
                continue

            combined = phone
            if description != "":
                combined = phone + " (" + description + ")"

            if atc_name in wanted or wanted in atc_name:
                if combined not in found:
                    found.append(combined)

    if len(found) == 0:
        return ""

    return " / ".join(found)


# ---------------------------------------------------------------------------
# main - build the json
# ---------------------------------------------------------------------------

print("loading phone numbers from " + ATC_FILE + " ...")
atc_list = load_atc_numbers(ATC_FILE)
print("loaded " + str(len(atc_list)) + " phone entries")
print("")

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

for airspace in root.findall(".//aixm:Airspace", NS):
    timeslice = airspace.find("aixm:timeSlice/aixm:AirspaceTimeSlice", NS)
    if timeslice is None:
        continue

    local_type_elem = timeslice.find("aixm:localType", NS)
    local_type = local_type_elem.text if local_type_elem is not None else None
    if local_type not in ("FRZ", "RPZ"):
        continue

    designator = (timeslice.findtext("aixm:designator", default="", namespaces=NS) or "").strip()
    raw_name = (timeslice.findtext("aixm:name", default="", namespaces=NS) or "").strip()
    name = get_display_name((designator + " " + raw_name).strip())

    note_texts = []
    for note in timeslice.findall(
        ".//aixm:Note/aixm:translatedNote/aixm:LinguisticNote/aixm:note", NS
    ):
        note_texts.append(note.text or "")

    phone = pull_phone_number(" ".join(note_texts))
    phone_source = "AIXM note"

    if phone == "No number available.":
        fallback = look_up_phone(name, atc_list)
        if fallback != "":
            phone = fallback
            phone_source = "atcadvisor.com"
            count_from_csv = count_from_csv + 1
        else:
            count_no_phone = count_no_phone + 1
            phone_source = "none"
    else:
        count_from_aixm = count_from_aixm + 1

    ring = build_ring(timeslice)
    if not ring:
        count_no_shape = count_no_shape + 1
        continue

    # round the coordinates to 5 decimal places - that's about 1 metre,
    # far more accurate than we need, and it makes the file much smaller
    tidy_ring = []
    for lon, lat in ring:
        tidy_ring.append([round(lon, 5), round(lat, 5)])

    zones.append({
        "name": name,
        "phone": phone,
        "phone_source": phone_source,
        "ring": tidy_ring,
    })

print("")
print("building " + OUTPUT_FILE + " ...")

data = {
    "airac_date": airac_date,
    "source_url": zip_url,
    "zone_count": len(zones),
    "zones": zones,
}

output_file = open(OUTPUT_FILE, "w", encoding="utf-8")
json.dump(data, output_file)
output_file.close()

print("")
print("done")
print("airac date:                " + airac_date)
print("zones written:             " + str(len(zones)))
print("phone from AIXM note:      " + str(count_from_aixm))
print("phone from atcadvisor csv: " + str(count_from_csv))
print("no phone number at all:    " + str(count_no_phone))
print("skipped (no shape):        " + str(count_no_shape))