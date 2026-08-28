# pulls current UK NOTAMs from the NATS PIB feed and writes notam_data.json
#
# UNLIKE make_website_data.py, this is NOT tied to the 28-day AIRAC cycle.
# NOTAMs change daily, and some ("trigger" NOTAMs) are only valid for two
# weeks - run this shortly before a survey, not once a month.
#
# source: https://www.nats.aero/do-it-online/pre-flight-information-bulletins/
# this is NATS' own contingency briefing feed, served with no login
# required. it is not a purpose-built API - treat it as unofficial and
# best-effort, and always cross-check against a proper NOTAM briefing
# before flying. see the disclaimer this script prints at the end.

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PIB_URL = "https://pibs.nats.co.uk/operational/pibs/PIB.xml"
OUTPUT_FILE = os.path.join(HERE, "notam_data.json")

# a NOTAM with no radius given defaults to this many nautical miles -
# EAD's convention for a "point" NOTAM
DEFAULT_RADIUS_NM = 1.0


# ---------------------------------------------------------------------------
# parsing the odd little formats NOTAMs use
# ---------------------------------------------------------------------------

COORD_RE = re.compile(r"^(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])$")


def parse_coordinates(text):
    # "5408N00316W" -> (lon, lat) in decimal degrees, or None if it
    # doesn't match the expected DDMM[N/S] DDDMM[E/W] shape
    if not text:
        return None

    match = COORD_RE.match(text.strip())
    if not match:
        return None

    lat_deg, lat_min, ns, lon_deg, lon_min, ew = match.groups()

    lat = int(lat_deg) + int(lat_min) / 60.0
    lon = int(lon_deg) + int(lon_min) / 60.0

    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon

    return round(lon, 5), round(lat, 5)


VALIDITY_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")


def parse_validity(text):
    # "2608010000" -> "2026-08-01T00:00:00", or None if unparseable.
    # NOTAMs also sometimes use "PERM" (permanent) or "EST" (estimated)
    # in place of a real end date - those aren't handled here and come
    # back as None, which the caller treats as "unknown, keep it anyway"
    if not text:
        return None

    match = VALIDITY_RE.match(text.strip())
    if not match:
        return None

    yy, mm, dd, hh, mi = match.groups()
    year = 2000 + int(yy)

    try:
        return f"{year:04d}-{mm}-{dd}T{hh}:{mi}:00"
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# download + parse
# ---------------------------------------------------------------------------

def download_pib():
    print("downloading NOTAM briefing: " + PIB_URL)
    resp = requests.get(PIB_URL, timeout=180)
    resp.raise_for_status()
    print("  downloaded " + str(round(len(resp.content) / 1024 / 1024, 1)) + " MB")
    return resp.content


def read_header(root):
    # top-level info about the briefing itself, not any one NOTAM
    header = root.find(".//AreaPIBHeader")
    if header is None:
        return {"issued": None, "valid_from": None, "valid_to": None}

    validity = header.find("Validity")
    return {
        "issued": header.findtext("Issued"),
        "valid_from": validity.findtext("ValidFrom") if validity is not None else None,
        "valid_to": validity.findtext("ValidTo") if validity is not None else None,
    }


def build_notam_entry(notam_elem):
    # one <Notam> element -> a small dict, or None if it can't usefully
    # be placed on a map (no parseable coordinates)
    series = notam_elem.findtext("Series", "")
    number = notam_elem.findtext("Number", "")
    year = notam_elem.findtext("Year", "")
    notam_id = (series + number + "/" + year).strip("/")

    coords_text = notam_elem.findtext("Coordinates", "")
    lonlat = parse_coordinates(coords_text)
    if lonlat is None:
        return None  # can't place this on a map - drop it

    lon, lat = lonlat

    radius_text = notam_elem.findtext("Radius", "")
    try:
        radius_nm = float(radius_text) if radius_text else DEFAULT_RADIUS_NM
    except ValueError:
        radius_nm = DEFAULT_RADIUS_NM
    radius_m = round(radius_nm * 1852.0)

    qline = notam_elem.find("QLine")
    lower_fl = qline.findtext("Lower") if qline is not None else None
    upper_fl = qline.findtext("Upper") if qline is not None else None

    return {
        "id": notam_id,
        "aerodrome": notam_elem.findtext("ItemA", "").strip(),
        "lat": lat,
        "lon": lon,
        "radius_m": radius_m,
        "lower_fl": lower_fl,
        "upper_fl": upper_fl,
        "start": parse_validity(notam_elem.findtext("StartValidity", "")),
        "end": parse_validity(notam_elem.findtext("EndValidity", "")),
        "text": (notam_elem.findtext("ItemE", "") or "").strip(),
    }


def parse_all_notams(xml_bytes):
    # deliberately doesn't rely on the FIRSection/ADSection nesting -
    # NOTAMs live under aerodrome sections, en-route sections, and
    # warning sections, each nested slightly differently. searching
    # for every <Notam> anywhere in the document is more robust than
    # assuming one fixed shape, and still gets everything.
    root = ET.fromstring(xml_bytes)

    header_info = read_header(root)

    notams = []
    skipped_no_coords = 0

    for notam_elem in root.findall(".//Notam"):
        entry = build_notam_entry(notam_elem)
        if entry is None:
            skipped_no_coords = skipped_no_coords + 1
            continue
        notams.append(entry)

    return header_info, notams, skipped_no_coords


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

xml_bytes = download_pib()

print("")
print("parsing NOTAMs ...")
header_info, notams, skipped_no_coords = parse_all_notams(xml_bytes)

data = {
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "disclaimer": (
        "UNOFFICIAL. Sourced from the NATS contingency briefing feed, not a "
        "purpose-built API - treat as best-effort. Trigger NOTAMs are valid "
        "for two weeks only. Always obtain a proper pre-flight NOTAM "
        "briefing before operating; do not rely on this list alone."
    ),
    "source_url": PIB_URL,
    "issued": header_info["issued"],
    "valid_from": header_info["valid_from"],
    "valid_to": header_info["valid_to"],
    "notam_count": len(notams),
    "notams": notams,
}

output_file = open(OUTPUT_FILE, "w", encoding="utf-8")
json.dump(data, output_file)
output_file.close()

print("")
print("done")
print("NOTAMs written:          " + str(len(notams)))
print("skipped (no coordinates): " + str(skipped_no_coords))
print("briefing issued:         " + str(header_info["issued"]))
print("valid:                   " + str(header_info["valid_from"]) + " to " + str(header_info["valid_to"]))
print("")
print("Reminder: run this again shortly before each survey. NOTAMs are")
print("time-sensitive in a way the 28-day AIRAC zone data is not.")