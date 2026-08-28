# downloads every uk atc page from atcadvisor.com AND every prison
# page from gov.uk, saving all the phone numbers into atc_numbers.csv
# run this on its own, then the frz script reads the csv it makes
#
# atcadvisor.com has no prison listings at all (it's an air traffic
# directory), so the HMP entries come from gov.uk instead - open
# government data, no login needed

import csv
import json
import time
import requests

SITE = "https://atcadvisor.com"
INDEX_URL = "https://atcadvisor.com/all-uk-atc-contacts"
OUTPUT_FILE = "atc_numbers.csv"
WAIT_SECONDS = 0.05  # small wait so we don't hammer their server

# gov.uk's official list of every prison in england and wales
GOV_SITE = "https://www.gov.uk"
PRISON_INDEX_URL = "https://www.gov.uk/government/collections/prisons-in-england-and-wales"
PRISON_WAIT_SECONDS = 0.05  # gov.uk gets a longer wait, be polite

# only prisons actually mentioned in this file get looked up - no
# point fetching all ~123 gov.uk prison pages when the frz data only
# ever needs a subset of them
FRZ_DATA_FILE = "frz_data.json"

# a handful of prisons - mostly the privately-run ones - either don't
# have a usable number on their gov.uk page, or the automated scrape
# just can't find it. these were looked up by hand rather than built
# into a scraper, since each private operator (Sodexo, Serco, G4S,
# MTC...) runs its own website with its own layout - not worth
# building and maintaining a separate scraper per operator for a
# dozen-ish prisons. only used as a fallback if the live scrape comes
# back empty, so a future gov.uk fix still takes priority automatically
MANUAL_PRISON_OVERRIDES = {
    "HMP ALTCOURSE": "0151 522 2000",
    "HMP ASHFIELD": "0117 303 8000",
    "HMP BRONZEFIELD": "01784 425690",
    "HMP DONCASTER": "01302 760 870",
    "HMP DOVEGATE": "01283 829400",
    "HMP FIVE WELLS": "01933 718888",
    "HMP FOREST BANK": "0161 925 7000",
    "HMP FOSSE WAY": "0116 216 2656",
    "HMP NORTHUMBERLAND": "01670 383100",
    "HMP OAKWOOD": "01902 799 700",
    "HMP PARC": "01656 300200",
    "HMP PETERBOROUGH": "01733 217500",
    "HMP RYE HILL": "01788 523300",
    "HMP THAMESIDE": "020 8317 9777",
    "HMP WETHERBY": "01937 544 385",
    "CHETWYND": "01939 250351", 
    "Keevil": "01264 784380 (ATC Switchboard)"
    # HMP DARTMOOR - no number available, left out on purpose rather
    # than guessing one
}

# pages on the site that aren't actual airfields
SKIP_PAGES = ["/", "/terms", "/privacy", "/all-uk-atc-contacts",
              "/all-uk-atc-contacts-map", ]


# downloads a web page and gives back the html as one long string
# tries twice in case the connection drops
def download_page(url):
    headers = {"User-Agent": "Mozilla/5.0"}

    attempt = 1
    while attempt <= 2:
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            return response.text
        except Exception as error:
            if attempt == 2:
                raise error
            time.sleep(3)
            attempt = attempt + 1


# pulls something like href="..." out of a tag
def get_attribute(tag, attribute_name):
    marker = attribute_name + '="'

    start = tag.find(marker)
    if start == -1:
        return ""

    start = start + len(marker)
    end = tag.find('"', start)
    if end == -1:
        return ""

    return tag[start:end]


# removes any html tags from a bit of text, e.g. an <img> inside a link
def strip_tags(text):
    clean = ""
    inside_tag = False

    i = 0
    while i < len(text):
        char = text[i]

        if char == "<":
            inside_tag = True
        elif char == ">":
            inside_tag = False
        elif inside_tag == False:
            clean = clean + char

        i = i + 1

    return clean.strip()


# grabs text inside brackets, e.g. (NATS Ltd) -> NATS Ltd
# stops if another tag starts first so we don't grab something unrelated
def get_bracketed_text(text):
    open_index = text.find("(")
    if open_index == -1:
        return ""
    # this used to cap at 40 chars to avoid grabbing an unrelated
    # distant bracket, but the site's real markup wraps the bracket in
    # a span tag that's about 45 chars on its own - 40 was cutting off
    # every legitimate case. 100 still catches genuinely unrelated
    # brackets further down the page.
    if open_index > 100:
        return ""

    next_link_index = text.find("<a ")
    if next_link_index != -1 and next_link_index < open_index:
        return ""

    depth = 0
    close_index = -1

    i = open_index
    while i < len(text):
        if text[i] == "(":
            depth = depth + 1
        elif text[i] == ")":
            depth = depth - 1
            if depth == 0:
                close_index = i
                break
        i = i + 1

    if close_index == -1:
        return ""

    inside = text[open_index + 1:close_index]
    return strip_tags(inside).strip()


# finds every airfield link on the index page
# gives back a list of [name, url] pairs
def find_airfield_links(html):
    links = []
    urls_already_seen = []

    search_from = 0
    while True:
        a_index = html.find("<a ", search_from)
        if a_index == -1:
            break

        tag_end = html.find(">", a_index)
        if tag_end == -1:
            break

        link_end = html.find("</a>", tag_end)
        if link_end == -1:
            break

        tag = html[a_index:tag_end]
        text = html[tag_end + 1:link_end]
        search_from = link_end + 4

        href = get_attribute(tag, "href")

        if href == "":
            continue

        # skip jump links like #list_a and other websites like nats.aero
        if href.startswith("#"):
            continue
        if href.startswith("http") and href.startswith(SITE) == False:
            continue

        # work out the full address and the bit after the domain
        # links can be written 3 different ways:
        #   "https://atcadvisor.com/some-page"   (full address)
        #   "/some-page"                         (starts from the root)
        #   "some-page"                          (relative, no slash at all)
        if href.startswith(SITE):
            url = href
            path = href[len(SITE):]
            if path.startswith("/") == False:
                path = "/" + path
        elif href.startswith("/"):
            url = SITE + href
            path = href
        else:
            url = SITE + "/" + href
            path = "/" + href

        if path == "" or path in SKIP_PAGES:
            continue

        path_without_slash = path.lstrip("/")

        not_a_page = ["tel:", "mailto:", "javascript:"]
        skip_this_one = False
        for prefix in not_a_page:
            if path_without_slash.startswith(prefix):
                skip_this_one = True
        if skip_this_one:
            continue

        name = strip_tags(text)
        if name == "" or name == "NSF":
            continue

        if url in urls_already_seen:
            continue

        urls_already_seen.append(url)
        links.append([name, url])

    return links


# finds every phone number on an airfield page
# gives back a list of [number, who it belongs to] pairs
def find_phone_numbers(html):
    numbers = []

    search_from = 0
    while True:
        marker = 'href="tel:'
        tel_index = html.find(marker, search_from)
        if tel_index == -1:
            break

        # the raw number sits between tel: and the closing quote
        number_start = tel_index + len(marker)
        number_end = html.find('"', number_start)
        number = html[number_start:number_end]

        tag_end = html.find(">", number_end)
        link_end = html.find("</a>", tag_end)
        if tag_end == -1 or link_end == -1:
            break

        # the tidy spaced version is the text shown on the page
        shown = strip_tags(html[tag_end + 1:link_end])
        if shown != "":
            number = shown

        # the company name is usually in brackets just after the link
        after_link = html[link_end + 4:link_end + 200]
        belongs_to = get_bracketed_text(after_link)
        description = belongs_to
        number_digits_only = number.replace(" ", "")
        if number_digits_only != "03300439373":
            numbers.append([number, belongs_to, description])
        search_from = link_end + 4

    return numbers


# the icao code is usually the last bit of the page address
# e.g. .../aberdeen-dyce-atc-egpd -> EGPD
def get_icao_from_url(url):
    last_part = url.split("/")[-1]
    bits = last_part.split("-")
    last_bit = bits[-1]

    if len(last_bit) == 4:
        return last_bit.upper()

    return ""


# ---------------------------------------------------------------------------
# prisons from gov.uk
# ---------------------------------------------------------------------------

# finds every prison link on gov.uk's collection page
# gives back a list of [name, url] pairs
def find_prison_links(html):
    links = []
    urls_already_seen = []

    search_from = 0
    while True:
        a_index = html.find("<a ", search_from)
        if a_index == -1:
            break

        tag_end = html.find(">", a_index)
        if tag_end == -1:
            break

        link_end = html.find("</a>", tag_end)
        if link_end == -1:
            break

        tag = html[a_index:tag_end]
        text = html[tag_end + 1:link_end]
        search_from = link_end + 4

        href = get_attribute(tag, "href")
        if href.startswith("/guidance/") == False:
            continue

        name = strip_tags(text)
        if name == "":
            continue

        # only the actual prison pages, not other guidance links
        upper_name = name.upper()
        is_prison = False
        if "PRISON" in upper_name:
            is_prison = True
        if "YOUNG OFFENDER" in upper_name:
            is_prison = True
        if is_prison == False:
            continue

        url = GOV_SITE + href
        if url in urls_already_seen:
            continue

        urls_already_seen.append(url)
        links.append([name, url])

    return links


# turns gov.uk's page title into the form the frz data uses
# "Downview Prison and Young Offender Institution" -> "HMP DOWNVIEW"
# "Werrington Young Offender Institution"          -> "HMP WERRINGTON"
def tidy_prison_name(name):
    name = name.strip()

    endings = [
        " Prison and Young Offender Institution",
        " Prison and Young Offender Institute",
        " Prison and Yoi",
        " Young Offender Institution",
        " Prison",
        " prison",
    ]

    for ending in endings:
        if name.endswith(ending):
            name = name[:len(name) - len(ending)]
            break

    return "HMP " + name.upper().strip()


# reads a phone number out of a bit of text, stopping cleanly
#
# the tricky bit: the page often has extra words right after the
# number, e.g. "Telephone: 01983 634 000\n24 hours" - so we stop
# once we've got 11 digits (a full uk number) rather than carrying
# on and swallowing the "24" from "24 hours"
def read_number_from(text):
    number = ""
    digit_count = 0
    started = False

    i = 0
    while i < len(text):
        char = text[i]

        if char.isdigit():
            number = number + char
            digit_count = digit_count + 1
            started = True

            if digit_count >= 11:
                break

        elif char == " ":
            if started:
                number = number + char
            # spaces before the number starts are just skipped

        elif char == "+" and started == False:
            number = number + char

        else:
            # anything else (a letter, a tag, a line break) ends it
            if started:
                break

        i = i + 1

    number = number.strip()

    # sanity check - a real uk number has at least 10 digits
    digits_only = ""
    for char in number:
        if char.isdigit():
            digits_only = digits_only + char

    if len(digits_only) < 10:
        return ""

    return number


# finds the main switchboard number on a prison's gov.uk page
#
# these pages have several numbers on them (booking lines, safer
# custody, health concerns), so we jump to the "Contact <name>"
# section near the bottom first - that's where the switchboard is
# finds the colon that actually belongs to a "Telephone" mention,
# even when there's a qualifier in between - e.g. "Telephone (24
# hours): 01777 862 000" or "Telephone (Monday to Friday): ...".
# a plain "Telephone:" search misses these entirely, since that exact
# substring never appears on those pages - the colon sits after the
# qualifier, not right after the word itself
def find_colon_after_telephone(html, search_from):
    word_index = html.find("Telephone", search_from)
    if word_index == -1:
        return -1, -1

    # the colon should be within a short distance - long enough for
    # "(24 hours)" or similar, not so long it reaches into unrelated text
    window_end = word_index + 40
    colon_index = html.find(":", word_index, window_end)

    if colon_index == -1:
        return -1, -1

    return word_index, colon_index


def find_prison_phone(html):
    # gov.uk gives the contact heading an id like id="contact-belmarsh"
    contact_index = html.find('id="contact')

    if contact_index != -1:
        word_index, colon_index = find_colon_after_telephone(html, contact_index)
        if colon_index != -1:
            after = html[colon_index + 1:colon_index + 81]
            phone = read_number_from(after)
            if phone != "":
                return phone

    # fallback - no contact section found (or nothing usable in it),
    # so look for the LAST "Telephone" mention on the page, which is
    # usually still the switchboard
    search_from = 0
    last_word_index = -1
    last_colon_index = -1

    while True:
        word_index, colon_index = find_colon_after_telephone(html, search_from)
        if word_index == -1:
            break
        last_word_index = word_index
        last_colon_index = colon_index
        search_from = word_index + 9  # length of "Telephone"

    if last_colon_index == -1:
        return ""

    after = html[last_colon_index + 1:last_colon_index + 81]
    return read_number_from(after)


# reads frz_data.json and works out which individual prison names it
# actually mentions, e.g. a zone called "HMP BELMARSH/THAMESIDE/ISIS"
# needs all three names looked up separately, since the "/" just
# means those prisons share one flight restriction zone
def get_needed_prison_names(path):
    try:
        data_file = open(path, encoding="utf-8")
    except Exception:
        print("warning: could not find " + path + " - will fetch every gov.uk prison instead")
        return None

    data = json.load(data_file)
    data_file.close()

    needed = set()

    for zone in data["zones"]:
        name = zone["name"]
        hmp_index = name.upper().find("HMP")
        if hmp_index == -1:
            continue

        after_hmp = name[hmp_index:]        # e.g. "HMP DOWNVIEW/HIGH DOWN"
        facility_part = after_hmp[4:]        # strip the "HMP " itself

        for piece in facility_part.split("/"):
            piece = piece.strip()
            if piece != "":
                needed.add("HMP " + piece)

    return needed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

print("getting the list of airfields...")
index_html = download_page(INDEX_URL)
airfields = find_airfield_links(index_html)
print("found " + str(len(airfields)) + " airfields")
print("")

rows = []
count = 0
with_number = 0
without_number = 0

for airfield in airfields:
    name = airfield[0]
    url = airfield[1]

    count = count + 1
    print("(" + str(count) + "/" + str(len(airfields)) + ") " + name)

    try:
        page_html = download_page(url)
        numbers = find_phone_numbers(page_html)
    except Exception as error:
        print("   could not download this one: " + str(error))
        numbers = []

    icao = get_icao_from_url(url)

    if len(numbers) == 0:
        without_number = without_number + 1
        rows.append([name, icao, "no number", "","", url])
    else:
        with_number = with_number + 1
        for number in numbers:
            phone = number[0]
            belongs_to = number[1]
            description = number [2]
            rows.append([name, icao, phone, belongs_to, description, url])

    time.sleep(WAIT_SECONDS)

# now the prisons from gov.uk - but only the ones frz_data.json
# actually mentions
print("")
print("working out which prisons are actually needed...")
needed_names = get_needed_prison_names(FRZ_DATA_FILE)

if needed_names is not None:
    print("need " + str(len(needed_names)) + " individual prisons")

print("")
print("getting the list of prisons from gov.uk...")
prison_index_html = download_page(PRISON_INDEX_URL)
all_prisons = find_prison_links(prison_index_html)
print("gov.uk lists " + str(len(all_prisons)) + " prisons in total")

# narrow that down to just the ones we need, if we know what those are
if needed_names is not None:
    prisons = []
    matched_names = set()

    for prison in all_prisons:
        gov_name = prison[0]
        tidied = tidy_prison_name(gov_name)

        if tidied in needed_names:
            prisons.append(prison)
            matched_names.add(tidied)

    not_found = needed_names - matched_names
else:
    prisons = all_prisons
    not_found = set()

print("will fetch " + str(len(prisons)) + " of them")
print("")

prisons_with_number = 0
prisons_without_number = 0
count = 0
overrides_used = set()

for prison in prisons:
    gov_name = prison[0]
    url = prison[1]

    count = count + 1
    print("(" + str(count) + "/" + str(len(prisons)) + ") " + gov_name)

    try:
        page_html = download_page(url)
        phone = find_prison_phone(page_html)
    except Exception as error:
        print("   could not download this one: " + str(error))
        phone = ""

    name = tidy_prison_name(gov_name)
    used_manual_override = False

    if phone == "" and name in MANUAL_PRISON_OVERRIDES:
        phone = MANUAL_PRISON_OVERRIDES[name]
        used_manual_override = True
        overrides_used.add(name)

    if phone == "":
        prisons_without_number = prisons_without_number + 1
        print("   no number found")
        rows.append([name, "", "no number", "", "", url])
    elif used_manual_override:
        prisons_with_number = prisons_with_number + 1
        print("   using manual number: " + phone)
        rows.append([name, "", phone, "HMPPS", "Switchboard (manual)", url])
    else:
        prisons_with_number = prisons_with_number + 1
        rows.append([name, "", phone, "HMPPS", "Switchboard", url])

    time.sleep(PRISON_WAIT_SECONDS)

# any override that never got used above means that prison's gov.uk
# page wasn't matched at all (rather than being matched but coming
# back empty) - add those in now so the number isn't silently missing
for override_name in MANUAL_PRISON_OVERRIDES:
    if override_name not in overrides_used:
        print(override_name + " - not matched via gov.uk at all, using manual number")
        rows.append([override_name, "", MANUAL_PRISON_OVERRIDES[override_name], "HMPPS", "Switchboard (manual)", ""])
        prisons_with_number = prisons_with_number + 1

# save everything to a csv
output_file = open(OUTPUT_FILE, "w", newline="", encoding="utf-8")
writer = csv.writer(output_file)
writer.writerow(["name", "icao", "phone", "belongs to", "description", "page"])

for row in rows:
    writer.writerow(row)

output_file.close()

print("")
print("done")
print("airfields with a number:    " + str(with_number))
print("airfields with no number:   " + str(without_number))
print("prisons with a number:      " + str(prisons_with_number))
print("prisons with no number:     " + str(prisons_without_number))

if len(not_found) > 0:
    print("")
    print(str(len(not_found)) + " needed prison(s) had no match on gov.uk at all:")
    for name in sorted(not_found):
        print("  " + name)
    print("(these might be in Scotland/NI, or just a place name in the")
    print(" zone title rather than a real second prison - worth checking by hand)")

print("")
print("lines saved to " + OUTPUT_FILE + ": " + str(len(rows)))