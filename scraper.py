#!/usr/bin/env python3
"""KioskPulse scraper — one comic ISSUE and one film every minute.

Every entry is its own issue, never a whole series: Action Comics #1 and
Action Comics #2 are two separate covers with two separate synopses.

    /home/amphibolite/Desktop/kiosk_files/
        comics/<publisher>/<year>/<series-vol-issue>.jpg
        comics/<publisher>/<year>/synopsis.txt
        posters/<year>/<slug>.jpg
        posters/<year>/synopsis.txt
        manifest.js      <- read by /home/amphibolite/Desktop/index.html
        state.json       <- recent history + ratios, survives reboots
        saved.txt        <- every source ever downloaded (never trimmed),
                            so nothing is ever downloaded twice

Comic issues come from the DC / Marvel wiki APIs (one page per issue).
Films come from Wikipedia. The pick order is random, but every group is
held to its target share: comics 50/50 DC-Marvel, 40% of comics from
2015 on, films 40/60 before/after 2000 (Filipino counts as "after"),
one Filipino film in ten.

Images: both CDNs now answer with WebP by default, so downloads are sniffed
and converted to JPEG (needs Pillow: sudo apt install python3-pil). Folders
are only created once a real image has been downloaded and checked.

Run:
    python3 scraper.py            # forever, one issue + one film per minute
    python3 scraper.py --once     # a single pair, for testing
"""

import io
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from PIL import Image
except ImportError:
    Image = None

# --------------------------------------------------------------------------
# locations — change these if you move things
# --------------------------------------------------------------------------
HOME = "/home/amphibolite/Desktop"
BASE = os.path.join(HOME, "kiosk_files")
POSTERS = os.path.join(BASE, "posters")
COMICS = os.path.join(BASE, "comics")
MANIFEST = os.path.join(BASE, "manifest.js")
STATE_FILE = os.path.join(BASE, "state.json")
SAVED_FILE = os.path.join(BASE, "saved.txt")   # /home/amphibolite/Desktop/kiosk_files/saved.txt

INTERVAL = 30            # seconds between passes (one issue + one film)
TIMEOUT = 20
MANIFEST_LIMIT = 1500    # newest entries listed for the display
ITEM_MEMORY = 1500       # entries kept in state.json for the manifest
HISTORY_MAX = 800        # recent history kept in state.json
SERIES_CACHE_DAYS = 30   # re-read an issue list after this many days
UA = ("Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36 KioskPulse/2.0")

WIKI = "https://en.wikipedia.org/wiki/"
SEARCH = "https://en.wikipedia.org/w/index.php?search=%s&ns0=1&limit=20"

# --------------------------------------------------------------------------
# comic series — one page per ISSUE lives in each volume's category
# --------------------------------------------------------------------------
WIKI_HOST = {"DC": "dc.fandom.com", "Marvel": "marvel.fandom.com"}

SERIES = {
    "DC": {
        "classic": ["Action Comics Vol 1", "Detective Comics Vol 1", "Batman Vol 1",
                    "Superman Vol 1", "Wonder Woman Vol 2", "The Flash Vol 2",
                    "Green Lantern Vol 4", "Swamp Thing Vol 2"],
        "modern": ["Batman Vol 3", "Wonder Woman Vol 5", "The Flash Vol 5",
                   "Superman Vol 4", "Justice League Vol 4", "Nightwing Vol 4",
                   "Harley Quinn Vol 3"],
    },
    "Marvel": {
        "classic": ["Amazing Spider-Man Vol 1", "Fantastic Four Vol 1", "Avengers Vol 1",
                    "Daredevil Vol 1", "Incredible Hulk Vol 1", "Uncanny X-Men Vol 1",
                    "Thor Vol 1", "Captain America Vol 1"],
        "modern": ["Amazing Spider-Man Vol 4", "Avengers Vol 8", "Thor Vol 5",
                   "Immortal Hulk Vol 1", "Daredevil Vol 6", "Fantastic Four Vol 6",
                   "Black Panther Vol 7", "Captain America Vol 9"],
    },
}

# target shares used to keep the mix on ratio while the order stays random
SHARE_PUBLISHER = {"DC": 0.5, "Marvel": 0.5}
SHARE_COMIC_ERA = {"classic": 0.6, "modern": 0.4}
SHARE_FILM_ERA = {"old": 0.4, "new": 0.6}   # Filipino films are counted as "new"
SHARE_FILIPINO = 0.10

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WIKI_IMG = re.compile(r"^https?://[\w.-]+wikimedia\.org/", re.I)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def fetch(url, binary=False, referer=None, tries=3):
    """HTTP GET with retries, so a flaky Pi Wi-Fi doesn't lose an item."""
    last = None
    for attempt in range(tries):
        headers = {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": ("image/avif,image/webp,image/png,image/*,*/*;q=0.8" if binary
                       else "text/html,application/json;q=0.9,*/*;q=0.8"),
        }
        # the Fandom CDN likes a Referer; Wikimedia does not need one
        if referer and ("fandom.com" in url or "wikia" in url):
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                data = res.read()
                if binary:
                    return data, res.headers.get("Content-Type", "")
                return data.decode("utf-8", "ignore")
        except Exception as exc:
            last = exc
            log("retry %d/%d (%s) %s" % (attempt + 1, tries, exc, url[-70:]))
            time.sleep(2 + attempt * 3)
    raise last


def fetch_json(url):
    return json.loads(fetch(url))


def clean(text):
    text = re.sub(r"<style[\s\S]*?</style>", " ", text or "", flags=re.I)
    text = re.sub(r"<sup[\s\S]*?</sup>", " ", text, flags=re.I)
    text = TAG_RE.sub(" ", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&ndash;", "-"), ("&mdash;", "-"), ("&hellip;", "\u2026")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def clean_wikitext(text):
    text = re.sub(r"<ref[^>]*/>", " ", text, flags=re.I)
    text = re.sub(r"<ref[^>]*>[\s\S]*?</ref>", " ", text, flags=re.I)
    text = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", " ", text, flags=re.I)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = TAG_RE.sub(" ", text)
    text = text.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", text).strip()


DISA_RE = re.compile(r"\s*\([^()]*\)\s*$")
YEAR_RE = re.compile(r"\b(19[2-9]\d|20\d{2})\b")
DIRECTOR_RE = re.compile(r"directed by ([A-Z][\w.'\-]*(?: [A-Z][\w.'\-]*){0,3})")


def display_title(title):
    """'Saga (comics)' -> 'Saga'."""
    return DISA_RE.sub("", title).strip() or title


def short(text, limit=240):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop > limit * 0.5:
        return cut[:stop + 1]
    return cut.rsplit(" ", 1)[0] + "\u2026"


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]
    return slug or "item"


def year_of(text, fallback=None):
    m = YEAR_RE.search(text or "")
    return m.group(1) if m else fallback


# --------------------------------------------------------------------------
# random order that still lands on the target ratio
# --------------------------------------------------------------------------
def ratio_pick(counts, targets):
    """Random choice, weighted towards whichever group is behind its share."""
    total = sum(counts.get(k, 0) for k in targets)
    pairs = []
    for key, target in targets.items():
        if total:
            pairs.append((key, max(0.12, target - counts.get(key, 0) / total + 0.30)))
        else:
            pairs.append((key, target))
    roll = random.uniform(0, sum(w for _, w in pairs))
    for key, weight in pairs:
        roll -= weight
        if roll <= 0:
            return key
    return pairs[-1][0]


def ratio_chance(counts, key, total_key, target):
    """Probability for a rare group (e.g. the Filipino film) — random, but
    it always catches up when it falls behind its share."""
    total = counts.get(total_key, 0)
    seen = counts.get(key, 0)
    deficit = target * (total + 1) - seen
    return max(0.0, min(1.0, deficit + random.uniform(-0.15, 0.15)))


# --------------------------------------------------------------------------
# comic issues — one page per issue on the DC / Marvel wikis
# --------------------------------------------------------------------------
def issue_titles(host, series, state):
    """Every issue page in a volume's category (cached in state.json)."""
    key = host + "|" + series
    cached = state["series"].get(key)
    if cached and cached.get("titles"):
        if time.time() - cached.get("ts", 0) < SERIES_CACHE_DAYS * 86400:
            return cached["titles"]

    pattern = re.compile(r"^%s \d+$" % re.escape(series))
    titles, cont = [], None
    for _ in range(3):
        url = ("https://%s/api.php?action=query&format=json&list=categorymembers"
               "&cmtype=page&cmlimit=500&cmtitle=%s"
               % (host, urllib.parse.quote("Category:" + series)))
        if cont:
            url += "&cmcontinue=" + urllib.parse.quote(cont)
        try:
            data = fetch_json(url)
        except Exception as exc:
            log("issue list failed for %s: %s" % (series, exc))
            break
        for member in ((data.get("query") or {}).get("categorymembers") or []):
            title = member.get("title", "")
            if pattern.match(title):
                titles.append(title)
        cont = (data.get("continue") or {}).get("cmcontinue")
        if not cont:
            break

    if titles:
        state["series"][key] = {"titles": titles[:1200], "ts": time.time()}
    return titles


def cover_jpeg(url):
    """The wiki CDN answers with WebP by default; ask for the original file,
    which comes back as JPEG/PNG."""
    if not url:
        return ""
    if "wikia.nocookie.net" in url:
        return url + ("&" if "?" in url else "?") + "format=original"
    return url if WIKI_IMG.match(url) else ""


def fetch_issue(host, publisher, page_title, modern):
    """One issue -> its own cover, year and synopsis."""
    url = ("https://%s/api.php?action=query&format=json&titles=%s"
           "&prop=pageimages%%7Crevisions&piprop=original%%7Cthumbnail&pithumbsize=480"
           "&rvprop=content&rvslots=main"
           % (host, urllib.parse.quote(page_title)))
    try:
        data = fetch_json(url)
    except Exception as exc:
        log("issue fetch failed for %s: %s" % (page_title, exc))
        return None

    pages = (data.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), None)
    if not page or "missing" in page:
        return None

    img = page.get("original") or page.get("thumbnail") or {}
    image = cover_jpeg(img.get("source"))
    if not image:
        return None

    rev = (page.get("revisions") or [{}])[0]
    text = ((rev.get("slots") or {}).get("main") or {}).get("*", "") or rev.get("*", "")

    year = None
    m = re.search(r"\|\s*Year\s*=\s*(\d{4})", text)
    if m:
        year = m.group(1)
    else:
        m = re.search(r"\|\s*Cover date\s*=[^\n|]*?(\d{4})", text)
        if m:
            year = m.group(1)
    if not year:
        return None
    if modern and int(year) < 2015:
        return None
    if not modern and int(year) >= 2015:
        return None

    syn = re.search(r"\|\s*Synopsis1\s*=\s*([\s\S]*?)(?=\n\s*\|\s*[A-Za-z0-9_]+\s*=|\n\}\})", text)
    body = clean_wikitext(syn.group(1)) if syn else ""
    if len(body) < 120:
        return None

    name = re.match(r"^(.*?) Vol (\d+) (\d+)$", page_title)
    if not name:
        return None

    return {
        "title": "%s #%d" % (name.group(1), int(name.group(3))),
        "slug": slugify("%s vol %s %s" % (name.group(1), name.group(2), name.group(3))),
        "format": "Comic Book",
        "year": year,
        "publisher": publisher,
        "sub": "%s Comics \u00b7 Vol %s" % (publisher, name.group(2)),
        "image": image,
        "synopsis": short(body),
        "source": "https://%s/wiki/%s" % (host, urllib.parse.quote(page_title.replace(" ", "_"))),
        "kind": "comic",
    }


def pick_comic(state, publisher, modern, used, budget=8):
    """A random issue from a random volume of that publisher/era."""
    host = WIKI_HOST[publisher]
    volumes = SERIES[publisher]["modern" if modern else "classic"]
    tries = 0

    for series in random.sample(volumes, len(volumes)):
        titles = issue_titles(host, series, state)
        fresh = [t for t in titles
                 if "https://%s/wiki/%s" % (host, urllib.parse.quote(t.replace(" ", "_"))) not in used]
        random.shuffle(fresh)
        for title in fresh[:6]:
            if tries >= budget:
                return None
            tries += 1
            item = fetch_issue(host, publisher, title, modern)
            if item:
                return item
    return None


# --------------------------------------------------------------------------
# films — Wikipedia article, poster and lead paragraph
# --------------------------------------------------------------------------
def article(title):
    return fetch(WIKI + urllib.parse.quote(title.replace(" ", "_")))


def og_image(html):
    """Any wikimedia host counts — posters now come from thumb.wikimedia.org
    as often as from upload.wikimedia.org."""
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if not m:
        return None
    url = m.group(1).split("?")[0]
    if not WIKI_IMG.match(url) or url.lower().endswith(".svg"):
        return None
    return url


def page_poster(title, size=480):
    """Poster straight from the wiki API — the same route the covers use.
    Covers the films whose article has no usable og:image."""
    url = ("https://en.wikipedia.org/w/api.php?action=query&format=json&titles=%s"
           "&prop=pageimages&piprop=thumbnail%%7Coriginal&pithumbsize=%d"
           % (urllib.parse.quote(title), size))
    try:
        data = fetch_json(url)
    except Exception as exc:
        log("poster lookup failed for %s: %s" % (title, exc))
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {}) or {}
    src = ((page.get("thumbnail") or {}).get("source")
           or (page.get("original") or {}).get("source"))
    if not src or src.lower().endswith(".svg"):
        return None
    return src


def lead_paragraph(html):
    start = html.find('id="mw-content-text"')
    if start < 0:
        return ""
    for m in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", html[start:start + 40000]):
        text = clean(m.group(1))
        if len(text) > 80 and not text.lower().startswith(("look up", "this article")):
            return text
    return ""


def links_on(html):
    found = set()
    for m in re.finditer(r'<a href="/wiki/([^"#:]+)"', html):
        title = urllib.parse.unquote(m.group(1)).replace("_", " ")
        if title == "Main Page" or "/" in title:
            continue
        found.add(title)
    return list(found)


def category(name):
    try:
        return links_on(fetch(WIKI + urllib.parse.quote("Category:" + name)))
    except Exception as exc:
        log("category %s failed: %s" % (name, exc))
        return []


def search_titles(query):
    try:
        return links_on(fetch(SEARCH % urllib.parse.quote(query)))
    except Exception as exc:
        log("search failed: %s" % exc)
        return []


ENGLISH_MARKET = re.compile(
    r"\b(American|British|Canadian|Australian|Irish|New Zealand|English)[\s-]")
NOT_ENGLISH = re.compile(
    r"Bollywood|Nollywood|Kollywood|Tollywood|Mollywood|Lollywood"
    r"|\b(?:Indian|Nigerian|Pakistani|Bangladeshi|Sri Lankan|Nepali|Ghanaian|Kenyan"
    r"|Egyptian|Moroccan|Turkish|Iranian|Israeli|Lebanese|Indonesian|Malaysian|Thai"
    r"|Vietnamese|Cambodian|Chinese|Hong Kong|Taiwanese|Japanese|Korean|Mongolian"
    r"|Russian|Ukrainian|Polish|Czech|Hungarian|Romanian|Bulgarian|Serbian|Croatian"
    r"|Greek|Swedish|Danish|Norwegian|Finnish|Icelandic|Dutch|Belgian|French|German"
    r"|Austrian|Swiss|Italian|Spanish|Portuguese|Brazilian|Mexican|Argentine|Colombian"
    r"|Chilean|Peruvian|Cuban|Venezuelan)\b"
    r"|(?:Hindi|Telugu|Tamil|Malayalam|Kannada|Bengali|Marathi|Punjabi|Gujarati"
    r"|Bhojpuri|Urdu|Malay|Mandarin|Cantonese|Persian|Arabic|Hebrew|Tagalog|Yoruba"
    r"|Hausa|Swahili|Zulu|Afrikaans|Sinhala)-language",
    re.I)
FILIPINO = re.compile(r"\b(Philippine|Philippines|Filipino|Tagalog)\b", re.I)


def pick_movie(state, modern, filipino, used):
    """One English-language film (or one Filipino film) with a poster."""
    for _ in range(3):
        if filipino:
            year = random.randint(2000, 2025)
            titles = search_titles('incategory:"%d films" Philippine' % year)
        else:
            year = random.randint(2000, 2025) if modern else random.randint(1940, 1999)
            titles = category("%d films" % year)

        random.shuffle(titles)
        for title in titles[:14]:
            source = WIKI + urllib.parse.quote(title.replace(" ", "_"))
            if source in used:
                continue
            try:
                html = article(title)
            except Exception:
                continue
            lead = lead_paragraph(html)
            if len(lead) < 80:
                continue
            if filipino:
                if not FILIPINO.search(lead[:260]):
                    continue
            elif not ENGLISH_MARKET.search(lead[:260]) or NOT_ENGLISH.search(lead[:260]):
                continue

            found_year = year_of(lead[:300], str(year))
            if not found_year:
                continue
            if filipino or modern:
                if int(found_year) < 2000:
                    continue
            elif int(found_year) >= 2000:
                continue

            image = og_image(html) or page_poster(title)
            if not image:
                continue

            director = DIRECTOR_RE.search(lead[:400])
            return {
                "title": display_title(title),
                "format": "Movie",
                "year": found_year,
                "publisher": "Filipino Cinema" if filipino else "",
                "sub": ("Dir. " + director.group(1)) if director else "Feature film",
                "image": image,
                "synopsis": short(lead),
                "source": source,
                "kind": "poster",
            }
        log("no film found for year run %d, retrying" % year)
    return None


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------
def grab_image(item):
    """Download the picture and normalise it to JPEG/PNG.

    Returns (bytes, extension) or (None, None). WebP is converted to JPEG so
    the Pi never has to decode it; if Pillow is missing the WebP is kept.
    """
    url = item["image"]
    candidates = list(dict.fromkeys([url, url.split("?")[0]]))

    for attempt in candidates:
        try:
            data, ctype = fetch(attempt, binary=True, referer=item.get("source"))
        except Exception as exc:
            log("%s: download failed (%s)" % (item["title"], exc))
            continue

        if data[:3] == b"\xff\xd8\xff":
            return data, "jpg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return data, "png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if Image:
                try:
                    buf = io.BytesIO()
                    Image.open(io.BytesIO(data)).convert("RGB").save(
                        buf, "JPEG", quality=88)
                    return buf.getvalue(), "jpg"
                except Exception as exc:
                    log("%s: webp -> jpeg failed (%s)" % (item["title"], exc))
            return data, "webp"
        log("%s: unexpected %s (%d bytes)" % (item["title"], ctype or "content", len(data)))

    return None, None


def save_media(item):
    """Download the image first, then create its folder and write it — so a
    failed download never leaves an empty subfolder behind."""
    data, ext = grab_image(item)
    if not data:
        log("no image for %s — nothing written" % item["title"])
        return None

    if item["kind"] == "comic":
        folder = os.path.join(COMICS, item["publisher"], str(item["year"]))
    else:
        folder = os.path.join(POSTERS, str(item["year"]))

    name = (item.get("slug") or slugify(item["title"])) + "." + ext
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(data)

    with open(os.path.join(folder, "synopsis.txt"), "a", encoding="utf-8") as fh:
        fh.write("=== %s (%s) ===\n" % (item["title"], item["year"]))
        fh.write("file: %s\n" % name)
        fh.write("source: %s\n" % item["source"])
        fh.write("%s\n\n" % item["synopsis"])

    item["image"] = os.path.relpath(path, HOME).replace(os.sep, "/")
    log("saved %s (%d KB) -> %s" % (item["title"], len(data) // 1024, item["image"]))
    return item


def write_manifest(items):
    data = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "items": items[-MANIFEST_LIMIT:],
    }
    payload = "window.KIOSK_MANIFEST = " + json.dumps(data, ensure_ascii=False) + ";\n"
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, MANIFEST)


def load_saved(state):
    """Permanent list of every source ever downloaded (never trimmed).

    Lives in kiosk_files/saved.txt, one URL per line. On the very first run
    it is rebuilt from state.json and from every synopsis.txt already on
    disk, so the existing library is covered too.
    """
    if os.path.exists(SAVED_FILE):
        with open(SAVED_FILE, encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}

    saved = set(state["history"])
    saved.update(i["source"] for i in state["items"] if i.get("source"))
    for root in (COMICS, POSTERS):
        for dirpath, _dirs, files in os.walk(root):
            if "synopsis.txt" not in files:
                continue
            with open(os.path.join(dirpath, "synopsis.txt"),
                      encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("source: "):
                        saved.add(line[8:].strip())

    with open(SAVED_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(saved)) + ("\n" if saved else ""))
    log("built saved.txt with %d entries" % len(saved))
    return saved


def load_state():
    state = {"counts": {}, "history": [], "items": [], "series": {}}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
            for key in state:
                if key in saved:
                    state[key] = saved[key]
        except Exception as exc:
            log("state.json unreadable (%s) — starting fresh" % exc)

    state["history"] = state["history"][-HISTORY_MAX:]
    state["used"] = load_saved(state)
    return state


def save_state(state):
    state["history"] = state["history"][-HISTORY_MAX:]
    state["items"] = state["items"][-ITEM_MEMORY:]
    out = {k: v for k, v in state.items() if k != "used"}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def remember(state, item):
    """Permanent record — an entry is never downloaded twice, even after it
    has dropped out of the 800-entry history in state.json."""
    key = item["source"]
    if key not in state["used"]:
        state["used"].add(key)
        state["history"].append(key)
        with open(SAVED_FILE, "a", encoding="utf-8") as fh:
            fh.write(key + "\n")
    state["items"].append(item)


# --------------------------------------------------------------------------
# one pass = one comic issue + one film
# --------------------------------------------------------------------------
def one_pass(state):
    counts = state["counts"]

    # ---- comic issue: random publisher/era, held to the target shares ----
    publisher = ratio_pick(counts, SHARE_PUBLISHER)
    era = ratio_pick(counts, SHARE_COMIC_ERA)
    comic = pick_comic(state, publisher, era == "modern", state["used"])

    if not comic and publisher == "DC":
        comic = pick_comic(state, "Marvel", era == "modern", state["used"])
        publisher = "Marvel"
    elif not comic:
        comic = pick_comic(state, "DC", era == "modern", state["used"])
        publisher = "DC"

    comic_saved = None
    if comic:
        try:
            comic_saved = save_media(comic)
        except Exception as exc:
            log("comic save failed: %s" % exc)
        if comic_saved:
            remember(state, comic_saved)
            counts[publisher] = counts.get(publisher, 0) + 1
            counts[era] = counts.get(era, 0) + 1

    time.sleep(random.uniform(2, 4))

    # ---- film: one in ten Filipino, the rest split before/after 2000 ----
    filipino = random.uniform(0, 1) < ratio_chance(
        counts, "filipino", "film", SHARE_FILIPINO)
    modern_film = True if filipino else ratio_pick(counts, SHARE_FILM_ERA) == "new"

    movie = pick_movie(state, modern_film, filipino, state["used"])
    if not movie and filipino:
        movie = pick_movie(state, True, False, state["used"])

    movie_saved = None
    if movie:
        try:
            movie_saved = save_media(movie)
        except Exception as exc:
            log("film save failed: %s" % exc)
        if movie_saved:
            remember(state, movie_saved)
            counts["film"] = counts.get("film", 0) + 1
            counts["new" if modern_film else "old"] = \
                counts.get("new" if modern_film else "old", 0) + 1
            if movie_saved["publisher"] == "Filipino Cinema":
                counts["filipino"] = counts.get("filipino", 0) + 1

    if comic_saved or movie_saved:
        write_manifest(state["items"])
        save_state(state)


def report(state):
    counts = state["counts"]
    films = counts.get("film", 0) or 0
    comics = counts.get("DC", 0) + counts.get("Marvel", 0)
    log("collected so far — comics %d (DC %d / Marvel %d, %d from 2015 on) · "
        "films %d (%d before 2000, %d Filipino) · saved.txt %d"
        % (comics, counts.get("DC", 0), counts.get("Marvel", 0), counts.get("modern", 0),
           films, counts.get("old", 0), counts.get("filipino", 0),
           len(state["used"])))


def main():
    os.makedirs(POSTERS, exist_ok=True)
    os.makedirs(COMICS, exist_ok=True)

    state = load_state()
    if not os.path.exists(MANIFEST):
        write_manifest(state["items"])
    log("kiosk_files ready at %s" % BASE)
    log("%d entries in manifest memory, %d in permanent saved.txt"
        % (len(state["items"]), len(state["used"])))
    if not Image:
        log("Pillow missing — WebP covers will be kept as .webp "
            "(install with: sudo apt install python3-pil)")

    once = "--once" in sys.argv
    while True:
        try:
            one_pass(state)
        except Exception as exc:          # keep the loop alive on the Pi
            log("pass failed: %s" % exc)
        report(state)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
