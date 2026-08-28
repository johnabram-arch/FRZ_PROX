# builds tower_data.json from the nget tower export csv
#
# run again whenever nget send an updated export, then upload the new
# tower_data.json to the site (same folder as frz_data.json and index.html)
#
# CSV COLUMNS EXPECTED (from nget's export):
#   X            -> longitude
#   Y            -> latitude
#   TOWER_ASSE   -> tower asset number, e.g. "4YG001" or "4TA001A"

import csv
import json

INPUT_FILE = "NGET Towers Database Filter - TowerExportWGS84.csv"
OUTPUT_FILE = "tower_data.json"


# splits "4TA001A" into "4TA", "001", "A" - the prefix can have digits
# in it too (4YG, 2AC), so we can't just split on the first digit -
# scan back from the end looking for exactly 3 digits instead
def split_tower_code(code):
    code = code.strip().upper()

    i = len(code) - 1
    while i >= 2:
        chunk = code[i - 2:i + 1]
        if chunk.isdigit():
            prefix = code[:i - 2]
            number = chunk
            suffix = code[i + 1:]
            return prefix, number, suffix
        i = i - 1

    return None, None, None


# base code (prefix + number, no suffix) -> towers sharing it
# e.g. "4TA001" -> ["4TA001", "4TA001A"]
tower_index = {}

seen_codes = set()

row_count = 0
skipped_count = 0
duplicate_count = 0

input_file = open(INPUT_FILE, encoding="utf-8-sig")
reader = csv.DictReader(input_file)

for row in reader:
    row_count = row_count + 1

    code = row.get("TOWER_ASSE", "").strip().upper()
    if code == "":
        skipped_count = skipped_count + 1
        continue

    try:
        lon = float(row["X"])
        lat = float(row["Y"])
    except Exception:
        skipped_count = skipped_count + 1
        continue

    prefix, number, suffix = split_tower_code(code)
    if prefix is None:
        skipped_count = skipped_count + 1
        continue

    base_code = prefix + number

    # same code + same coords already seen - don't list it twice
    dedupe_key = code + "|" + str(round(lon, 6)) + "|" + str(round(lat, 6))
    if dedupe_key in seen_codes:
        duplicate_count = duplicate_count + 1
        continue
    seen_codes.add(dedupe_key)

    if base_code not in tower_index:
        tower_index[base_code] = []

    tower_index[base_code].append({
        "code": code,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
    })

input_file.close()

output_file = open(OUTPUT_FILE, "w", encoding="utf-8")
json.dump(tower_index, output_file)
output_file.close()

# tally up how many towers actually got written
total_towers = 0
for towers_at_this_code in tower_index.values():
    total_towers = total_towers + len(towers_at_this_code)

print("done")
print("rows read:              " + str(row_count))
print("rows skipped (bad data): " + str(skipped_count))
print("exact duplicates merged: " + str(duplicate_count))
print("towers written:          " + str(total_towers))
print("distinct base codes:     " + str(len(tower_index)))