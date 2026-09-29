import asyncio
import base64
import pathlib
import random
import re
from urllib.parse import quote_plus

import requests
from playwright.async_api import async_playwright

# ----------------------------- settings -----------------------------
KEYWORDS_FILE = pathlib.Path('keywords.txt')   # one keyword per line
COUNT_FILE = pathlib.Path('count.txt')         # see notes below
QUERY_TEMPLATE = 'Number and Email address of {keyword} hospital'
OUT = pathlib.Path('images')                   # ONE folder for everything
MAX_SCROLLS = 10                               # safety limit per keyword
PAUSE_BETWEEN_KEYWORDS = (2, 4)                # random seconds, helps avoid blocks
# --------------------------------------------------------------------

OUT.mkdir(exist_ok=True)

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                         'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
EXT = {'jpeg': 'jpg', 'jpg': 'jpg', 'png': 'png', 'webp': 'webp', 'gif': 'gif'}


# ------------------------------ input files ------------------------------
def read_lines(path: pathlib.Path) -> list[str]:
    if not path.is_file():
        raise SystemExit(f'File not found: {path.resolve()}')
    with path.open(encoding='utf-8') as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith('#')]


def read_counts(path: pathlib.Path) -> list[int]:
    """count.txt: one number  -> used for every keyword.
                  many numbers -> line i is the count for keyword i
                                  (last number is reused if there are fewer lines)."""
    counts = []
    for ln in read_lines(path):
        try:
            n = int(ln.split()[0])
        except ValueError:
            raise SystemExit(f'Bad line in {path}: "{ln}" (expected a whole number)')
        if n <= 0:
            raise SystemExit(f'Counts must be > 0, got {n}')
        counts.append(n)
    if not counts:
        raise SystemExit(f'{path} is empty')
    return counts


# ------------------------------ image helpers ------------------------------
def is_result(src: str) -> bool:
    """Keep real result thumbnails, skip Google UI icons / 1x1 placeholders."""
    if src.startswith('data:image/'):
        kind = src[11:src.find(';')]
        return kind in EXT and len(src) > 2000
    return src.startswith('https://encrypted-tbn')


def next_index(safe: str) -> int:
    """Continue numbering if this keyword already has files in the folder."""
    highest = 0
    for p in OUT.glob(f'{safe}_*'):
        m = re.fullmatch(rf'{re.escape(safe)}_(\d+)', p.stem)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def save_image(src: str, safe: str, index: int) -> pathlib.Path:
    if src.startswith('data:image/'):
        header, b64 = src.split(',', 1)
        ext = EXT[header[11:header.find(';')]]
        data = base64.b64decode(b64)
    else:
        r = requests.get(src, headers=HEADERS, timeout=10)
        r.raise_for_status()
        ctype = r.headers.get('content-type', 'image/jpeg').split('/')[-1].split(';')[0]
        ext = EXT.get(ctype, 'jpg')
        data = r.content
    if not data:
        raise ValueError('empty image')
    path = OUT / f'{safe}_{index:03d}.{ext}'
    path.write_bytes(data)
    return path


async def collect_srcs(page, want: int) -> list[str]:
    found, seen = [], set()
    for _ in range(MAX_SCROLLS):
        srcs = await page.locator('img').evaluate_all(
            "els => els.map(e => e.currentSrc || e.src || e.getAttribute('data-src') || '')"
        )
        for s in srcs:
            if s and s not in seen and is_result(s):
                seen.add(s)
                found.append(s)
        if len(found) >= want:
            break
        await page.evaluate('window.scrollBy(0, window.innerHeight)')
        await page.wait_for_timeout(1600)
    return found


# ------------------------------ captcha handling ------------------------------
async def is_captcha(page) -> bool:
    """True if Google is showing its 'unusual traffic' / captcha page."""
    if '/sorry/' in page.url:
        return True
    try:
        return await page.locator(
            'iframe[src*="recaptcha"], form#captcha-form, #recaptcha'
        ).count() > 0
    except Exception:          # page was mid-navigation; treat as not blocked for now
        return False


async def wait_for_captcha(page, url: str) -> None:
    """If a captcha is showing, pause until the user solves it in the browser window."""
    if not await is_captcha(page):
        return
    print('\n  >>> CAPTCHA detected. Solve it in the browser window.'
          '\n  >>> The script will continue automatically once it is solved...')
    while await is_captcha(page):
        await asyncio.sleep(1)
    await page.wait_for_load_state('domcontentloaded')
    await asyncio.sleep(1.5)
    print('  Captcha cleared, continuing.\n')
    # Sometimes Google lands on normal web results after the captcha; go back to images.
    if 'tbm=isch' not in page.url and 'udm=2' not in page.url:
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)


# ------------------------------ one keyword ------------------------------
async def process_keyword(page, keyword: str, want: int) -> int:
    query = QUERY_TEMPLATE.format(keyword=keyword)
    url = f'https://www.google.com/search?q={quote_plus(query)}&tbm=isch&hl=en'
    print(f'\n=== "{keyword}"  (want {want}) ===')
    print(f'Query: {query}')

    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    await wait_for_captcha(page, url)          # pauses here until you solve it
    await page.wait_for_selector('img', state='attached', timeout=15000)

    srcs = await collect_srcs(page, want)
    print(f'Found {len(srcs)} candidate images.')

    safe = re.sub(r'\W+', '_', keyword).strip('_') or 'keyword'
    idx = next_index(safe)
    saved = 0
    for src in srcs:
        if saved >= want:
            break
        try:
            path = save_image(src, safe, idx)
            idx += 1
            saved += 1
            print(f'  saved {path.name}')
        except Exception as e:
            print(f'  skipped one image: {e}')
    return saved


# ---------------------------------- main ----------------------------------
async def main():
    keywords = read_lines(KEYWORDS_FILE)
    counts = read_counts(COUNT_FILE)
    if not keywords:
        raise SystemExit(f'{KEYWORDS_FILE} is empty')

    total = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        for i, keyword in enumerate(keywords):
            want = counts[i] if i < len(counts) else counts[-1]
            try:
                total += await process_keyword(page, keyword, want)
            except Exception as e:
                # one bad keyword shouldn't kill the whole run
                print(f'  !! failed on "{keyword}": {e}')
            if i < len(keywords) - 1:
                await asyncio.sleep(random.uniform(*PAUSE_BETWEEN_KEYWORDS))

        await browser.close()

    print(f'\nAll done. {total} image(s) saved in {OUT.resolve()}')


asyncio.run(main())