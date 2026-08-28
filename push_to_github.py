# commits and pushes the refreshed JSON files to the repo that GitHub
# Pages serves from. relies on git already being able to push from this
# machine - if `git push` works when you type it by hand in this folder,
# it'll work here too, since this script just runs the same commands.
#
# only commits frz_data.json, airstrip_data.json and notam_data.json -
# never `git add .` - so it can't accidentally sweep up some half-finished
# edit to index.html you happen to have sitting in the same folder.
#
# if nothing actually changed since the last run (e.g. the AIRAC data
# hasn't rolled over yet), it skips the commit and push entirely rather
# than creating an empty commit every week.

import subprocess
import sys
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

FILES_TO_PUSH = [
    "frz_data.json",
    "airstrip_data.json",
    "notam_data.json",
]


def run_git(args):
    # runs a git command in HERE, returns (exit_code, stdout, stderr)
    result = subprocess.run(
        ["git"] + args,
        cwd=HERE,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def check_is_git_repo():
    code, out, err = run_git(["rev-parse", "--is-inside-work-tree"])
    return code == 0


def files_present():
    present = [f for f in FILES_TO_PUSH if os.path.exists(os.path.join(HERE, f))]
    missing = [f for f in FILES_TO_PUSH if f not in present]
    return present, missing


def has_changes(present_files):
    # `git status --porcelain <files>` prints one line per changed file,
    # nothing at all if there's nothing to commit
    code, out, err = run_git(["status", "--porcelain", "--"] + present_files)
    return bool(out)


def main():
    if not check_is_git_repo():
        print("*** " + HERE + " doesn't look like a git repo - is this script in the right folder? ***")
        return False

    present_files, missing_files = files_present()

    if missing_files:
        print("note: " + ", ".join(missing_files) + " not found locally - run the refresh scripts first")

    if not present_files:
        print("*** none of the expected data files were found - nothing to push ***")
        return False

    if not has_changes(present_files):
        print("no changes to " + ", ".join(present_files) + " - nothing to commit, skipping push")
        return True

    print("changes detected in: " + ", ".join(present_files))

    code, out, err = run_git(["add", "--"] + present_files)
    if code != 0:
        print("*** git add failed: " + err + " ***")
        return False

    commit_message = "Auto-refresh data - " + datetime.now().strftime("%Y-%m-%d %H:%M")
    code, out, err = run_git(["commit", "-m", commit_message])
    if code != 0:
        print("*** git commit failed: " + err + " ***")
        return False
    print("committed: " + commit_message)

    code, out, err = run_git(["push"])
    if code != 0:
        print("*** git push failed: " + err + " ***")
        print("*** the commit was made locally but did NOT reach GitHub - the live site is still stale ***")
        print("*** common causes: no internet, needs a `git pull` first, or your credentials have expired ***")
        return False

    print("pushed to GitHub. Pages will pick this up automatically, usually within a minute or so.")
    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
