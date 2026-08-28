# pulls the main switchboard number for every prison in england and
# wales, straight from the official gov.uk pages
#
# data source: gov.uk/government/collections/prisons-in-england-and-wales
# this is genuinely open government data (Open Government Licence v3.0)
# with no login and no anti-scraping stance - unlike ukcell.net, gov.uk
# actively wants this content reused
#
# each prison's own page has a "Contact <name>" section near the
# bottom with its main switchboard number - that's what this pulls.
# note: this is a first pass based on one page's structure (belmarsh) -
# if a prison's page is laid out differently this might miss it, so
# check the "no number found" count when it finishes

import csv
import re
import time

import requests

COLLECTION_URL = "https://www.gov.uk/government/collections/prisons-in-england-and-wales"
SITE = "https://www.gov.uk"
OUTPUT_FILE = "prison_numbers.csv"
WAIT_SECONDS = 0.01  # be polite to gov.uk's servers


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


# finds every link to a /guidance/...-prison page on the collection
# page, along with the prison's display name
def find_prison_links(html):
    links = []
    seen_urls = []

    matches = re.findall(
        r'<a[^>]+href="(/guidance/[^"]+)"[^>]*>([^<]+)</a>',
        html,
    )

    for href, name in matches:
        if "prison" not in href and "yoi" not in href:
            continue  # skip any unrelated /guidance/ links on the page

        url = SITE + href

        if url in seen_urls:
            continue
        seen_urls.append(url)

        clean_name = name.strip()
        links.append([clean_name, url])

    return links


# finds the main switchboard number on a prison's own page.
#
# each page has several phone numbers (booking lines, safer custody,
# health concerns, etc) - the actual switchboard is in a "Contact
# <name>" section right near the end of the page, so we take the
# LAST "Telephone:" on the page rather than the first one
def find_main_phone_number(html):
    matches = re.findall(r"Telephone:\s*</?[a-z]*>?\s*([0-9][0-9 ]{8,14}[0-9])", html)

    if len(matches) == 0:
        return ""

    return matches[-1].strip()


print("getting the list of prisons...")
collection_html = download_page(COLLECTION_URL)
prisons = find_prison_links(collection_html)
print("found " + str(len(prisons)) + " prisons")
print("")

rows = []
count = 0
with_number = 0
without_number = 0

for prison in prisons:
    name = prison[0]
    url = prison[1]

    count = count + 1
    print("(" + str(count) + "/" + str(len(prisons)) + ") " + name)

    try:
        page_html = download_page(url)
        phone = find_main_phone_number(page_html)
    except Exception as error:
        print("   could not download this one: " + str(error))
        phone = ""

    if phone == "":
        without_number = without_number + 1
        rows.append([name, "no number found", url])
    else:
        with_number = with_number + 1
        rows.append([name, phone, url])

    time.sleep(WAIT_SECONDS)

output_file = open(OUTPUT_FILE, "w", newline="", encoding="utf-8")
writer = csv.writer(output_file)
writer.writerow(["name", "phone", "page"])
for row in rows:
    writer.writerow(row)
output_file.close()

print("")
print("done")
print("prisons with a number:    " + str(with_number))
print("prisons with no number:   " + str(without_number))
print("lines saved to " + OUTPUT_FILE + ": " + str(len(rows)))