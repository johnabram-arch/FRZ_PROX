# runs the whole data pipeline in one go:
#   1. ATC_NUMBERS_1_2.py   - refresh the phone number CSV
#   2. make_website_data.py - refresh frz_data.json + airstrip_data.json (AIRAC, every 28 days)
#   3. fetch_notams.py      - refresh notam_data.json (NOTAMs, changes daily)
#
# safe to run weekly (or daily) even though the AIRAC data only changes
# every 28 days - if nothing's changed, make_website_data.py just downloads
# the same file again and writes the same output. no harm, just a bit of
# wasted bandwidth.
#
# each step is wrapped separately: if one script fails (e.g. NATS is down),
# the others still run rather than the whole refresh stopping dead.
#
# set this up in Windows Task Scheduler (see setup_scheduled_task.ps1) and
# you never have to remember to run this by hand again.

import subprocess
import sys
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable  # use whichever python is running this script

STEPS = [
    ("ATC_NUMBERS_1_2.py", "phone number list"),
    ("make_website_data.py", "FRZ/RPZ zones + advisory airstrips (AIRAC)"),
    ("pull_notams.py", "NOTAMs"),
    ("push_to_github.py", "commit + push to GitHub Pages"),
]


def run_step(script_name, description):
    script_path = os.path.join(HERE, script_name)

    print("")
    print("=" * 70)
    print(description.upper() + "  (" + script_name + ")")
    print("=" * 70)

    if not os.path.exists(script_path):
        print("SKIPPED - " + script_name + " not found in " + HERE)
        return False

    result = subprocess.run([PYTHON, script_path], cwd=HERE)

    if result.returncode != 0:
        print("")
        print("*** " + script_name + " FAILED (exit code " + str(result.returncode) + ") ***")
        print("*** the other steps will still run, but check this before your next survey ***")
        return False

    return True


print("FRZ tool data refresh - started " + datetime.now().strftime("%Y-%m-%d %H:%M"))

results = {}
for script_name, description in STEPS:
    results[script_name] = run_step(script_name, description)

print("")
print("=" * 70)
print("SUMMARY")
print("=" * 70)

all_ok = True
for script_name, description in STEPS:
    ok = results.get(script_name, False)
    status = "OK" if ok else "FAILED / SKIPPED"
    print("  " + status.ljust(18) + description)
    if not ok:
        all_ok = False

print("")
if all_ok:
    print("All good - data refreshed, committed, and pushed to GitHub Pages.")
else:
    print("One or more steps failed - see above. Don't rely on this data for a")
    print("survey until it's been refreshed successfully. If only the push step")
    print("failed, the local files are still fine - the live site just wasn't updated.")

sys.exit(0 if all_ok else 1)