#!/usr/bin/env python3
# normalize_playlist.sh — IPTV M3U normalizer (keeps ALL channels, no deduplication)
# Usage: ./normalize_playlist.sh [input.m3u8] [output.m3u]
#   Defaults: input=playlist.m3u8, output=playlist_normalized.m3u (same dir as script)

import re
import sys
import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

INPUT_FILE  = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "playlist.m3u8")
OUTPUT_FILE = sys.argv[2] if len(sys.argv) > 2 else os.path.join(SCRIPT_DIR, "playlist_normalized.m3u")

GROUP_ORDER = [
    "Israel", "Russia", "Ukraine", "USA",
    "News", "Sports", "Movies", "Kids",
    "Music", "Documentary", "Entertainment",
    "Adults", "Other",
]

# ---------------------------------------------------------------------------
# Cyrillic → Latin transliteration table (for tvg-id generation)
# ---------------------------------------------------------------------------

TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts',
    'ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu',
    'я':'ya',
    'А':'a','Б':'b','В':'v','Г':'g','Д':'d','Е':'e','Ё':'yo','Ж':'zh',
    'З':'z','И':'i','Й':'y','К':'k','Л':'l','М':'m','Н':'n','О':'o',
    'П':'p','Р':'r','С':'s','Т':'t','У':'u','Ф':'f','Х':'kh','Ц':'ts',
    'Ч':'ch','Ш':'sh','Щ':'shch','Ъ':'','Ы':'y','Ь':'','Э':'e','Ю':'yu',
    'Я':'ya',
}

def transliterate(text):
    return ''.join(TRANSLIT.get(c, c) for c in text)

def make_tvg_id(group, name):
    slug = transliterate(name).lower()
    slug = re.sub(r'[^a-z0-9]+', '.', slug).strip('.')
    slug = re.sub(r'\.{2,}', '.', slug)[:40]
    prefix = group.lower()
    return f"{prefix}.{slug}"

# ---------------------------------------------------------------------------
# Name cleanup (keep quality markers — user wants NO removal)
# ---------------------------------------------------------------------------

def clean_name(name):
    # Strip junk characters but keep content (HD, FHD, 4K, 50fps, etc.)
    name = name.strip()
    # Remove leading/trailing non-printable or weird punctuation
    name = re.sub(r'^[\|\-\*\#\s]+', '', name)
    name = re.sub(r'[\|\-\*\#\s]+$', '', name)
    return name.strip()

# ---------------------------------------------------------------------------
# Group classification
# ---------------------------------------------------------------------------

# Source group → target group (direct mapping where unambiguous)
EXTGRP_MAP = {
    'ישראלי':       'Israel',
    'взрослые':     'Adults',
    'usa':          'USA',
    'детские':      'Kids',
    'музыка':       'Music',
    'кино':         'Movies',
    'спорт':        'Sports',
    'новости':      'News',
    'познавательные': 'Documentary',
    'развлекательные': 'Entertainment',
}

# Keyword patterns for name-based classification (for HD, HD Orig, 4K, другие, etc.)
CLASSIFY_RULES = [
    # Israel — Hebrew chars or known channels
    ('Israel', re.compile(
        r'[\u05d0-\u05ea]|'
        r'\b(kan|keshet|reshet|hot|beep|13tv|sport5|ilive|israel|iltv|'
        r'i24|artv|knesset|makan|galatz|galgalatz|radio.?nat|'
        r'ch(11|12|13|14|22|23|24|33))\b',
        re.I
    )),
    # USA
    ('USA', re.compile(
        r'\b(abc|nbc|cbs|fox|cnn|hbo|espn|amc|usa\s*net|showtime|starz|'
        r'discovery\s*us|history\s*us|comedy\s*central|nickelodeon|'
        r'cartoon\s*network|disney\s*us|pbs|tbs|tnt|hgtv|food\s*net|'
        r'bravo|syfy|ftv|a&e|lifetime|hallmark|tlc|travel\s*ch|'
        r'nat\s*geo\s*us|animal\s*planet|bloomberg\s*us)\b',
        re.I
    )),
    # Sports
    ('Sports', re.compile(
        r'\b(sport|футбол|хоккей|tennis|теннис|match|матч|баскет|basket|'
        r'formula|формула|moto\s*gp|nba|nfl|nhl|mlb|eurosport|bein|'
        r'dazn|setanta|fighting|бокс|boxing|wrestl|ufc|olympic|олимп|'
        r'golf|гольф|swim|плаван|cycling|велос|racing|гонки|arena\s*sport|'
        r'super\s*sport|sport\s*\d|laliga|bundesliga|premier\s*league|'
        r'serie\s*a|ligue\s*1|champions)\b',
        re.I
    )),
    # News
    ('News', re.compile(
        r'\b(news|новости|новост|вести|cnn|bbc\s*news|al\s*jazeera|'
        r'euronews|france\s*24|dw|rt\b|ntv\s*mir|mir\b|tass|'
        r'первый\s*канал|rossiya|россия\s*24|россия\s*1|'
        r'channel\s*one|один|fox\s*news|msnbc|sky\s*news)\b',
        re.I
    )),
    # Kids
    ('Kids', re.compile(
        r'\b(kids|детск|children|cartoon|мульт|nickelodeon|nick\b|'
        r'disney\s*junior|disney\s*jr|baby\s*tv|junior|jr\b|'
        r'tiji|puls[ik]|boomerang|cn\b|treehouse|sprout|'
        r'мир\s*детей|телепузик|luntik|лунтик|фиксик|smesharik|'
        r'tom\s*&\s*jerry|смешарик|мульт[ик]|малышарик)\b',
        re.I
    )),
    # Music
    ('Music', re.compile(
        r'\b(music|музык|муз\.?тв|mtv\b|vh1|mezzo|classica|'
        r'hit\s*tv|record\s*tv|europa\s*plus|авторадио|наше\s*радио|'
        r'bridge\s*tv|ultra\s*hd\s*music|dance\s*tv|m2\b|m-1|'
        r'ru\.?tv|жара|мелодия|ретро\s*фм|радио)\b',
        re.I
    )),
    # Documentary
    ('Documentary', re.compile(
        r'\b(docu|познавател|discovery|national\s*geo|nat\.?\s*geo|'
        r'history\b|viasat\s*history|science|наука|animal\s*planet|'
        r'охота|рыбалк|hunting|fishing|explore|investigate|crime|'
        r'криминал|загадк|тайн|nature|природа|geo\s*tv|тв\s*3|'
        r'планета|путешеств|travel)\b',
        re.I
    )),
    # Movies
    ('Movies', re.compile(
        r'\b(кино|cinema|movie|film|ivi|amedia|premier|more\.?tv|'
        r'hd\s*hit|hd\s*life|star\s*cinema|serial|сериал|super|'
        r'thriller|horror|action|comedy|drama|мелодрам|детектив|'
        r'fox\s*life|tcm|hallmark\s*movie|tnt\s*series|вий\s*тв|'
        r'megogo|okko|kinopoisk|netflix|start\s*tv|more\b)\b',
        re.I
    )),
    # Ukraine
    ('Ukraine', re.compile(
        r'\b(украин|ukraine|1\+1|2\+2|стб\b|ictv\b|новий\s*канал|'
        r'тет\b|прямий|espreso|ukraine\s*24|uatv|bigudi|'
        r'kanal\s*ukraine|пятница|украина\b|unian)\b',
        re.I
    )),
]

# Heuristic: majority Cyrillic → Russia (last resort before Other)
def is_mostly_cyrillic(text):
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    cyrillic = sum(1 for c in alpha if '\u0400' <= c <= '\u04ff')
    return cyrillic / len(alpha) > 0.5

def classify(extgrp_raw, name):
    """Return target group name for a channel."""
    extgrp = extgrp_raw.strip().lower() if extgrp_raw else ''

    # Direct mapping from source group
    if extgrp in EXTGRP_MAP:
        return EXTGRP_MAP[extgrp]

    # Name-based rules (for HD, HD Orig, 4K, другие, развлекательные, etc.)
    for group, pattern in CLASSIFY_RULES:
        if pattern.search(name):
            return group

    # Cyrillic fallback → Russia
    if is_mostly_cyrillic(name):
        return 'Russia'

    return 'Other'

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_playlist(path):
    """Yield dicts: {extinf_raw, extgrp_raw, name, url}"""
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = [l.rstrip('\n').rstrip('\r') for l in f]

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith('#EXTINF'):
            i += 1
            continue

        extinf_raw = line
        # Extract display name (after last comma)
        m = re.search(r',(.+)$', line)
        raw_name = m.group(1) if m else 'Unknown'

        # Next lines: optional EXTGRP then URL
        extgrp_raw = ''
        url = ''
        j = i + 1
        while j < len(lines) and not lines[j].startswith('#EXTINF'):
            l = lines[j]
            if l.startswith('#EXTGRP:'):
                extgrp_raw = l[len('#EXTGRP:'):]
            elif l and not l.startswith('#'):
                url = l.strip()
            j += 1

        i = j  # advance past this channel block

        if not url:
            continue  # skip entries without a URL

        yield {
            'extinf_raw': extinf_raw,
            'extgrp_raw': extgrp_raw,
            'raw_name':   raw_name,
            'url':        url,
        }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: Input file not found: {INPUT_FILE}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading: {INPUT_FILE}", file=sys.stderr)
    channels = list(parse_playlist(INPUT_FILE))
    print(f"Parsed {len(channels)} channels", file=sys.stderr)

    # Classify and enrich each channel
    for ch in channels:
        ch['group']   = classify(ch['extgrp_raw'], ch['raw_name'])
        ch['name']    = clean_name(ch['raw_name'])
        ch['tvg_id']  = make_tvg_id(ch['group'], ch['name'])
        ch['tvg_logo'] = ''  # source has no logos

    # Sort: by GROUP_ORDER index, then by name within group
    group_rank = {g: i for i, g in enumerate(GROUP_ORDER)}

    channels.sort(key=lambda c: (
        group_rank.get(c['group'], len(GROUP_ORDER)),
        c['name'].lower()
    ))

    # Assign tvg-chno per group (sequential, 1-based)
    chno_counter = {}
    for ch in channels:
        g = ch['group']
        chno_counter[g] = chno_counter.get(g, 0) + 1
        ch['tvg_chno'] = chno_counter[g]

    # Write output
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for ch in channels:
            f.write(
                f'#EXTINF:-1 '
                f'tvg-id="{ch["tvg_id"]}" '
                f'tvg-name="{ch["name"]}" '
                f'tvg-chno="{ch["tvg_chno"]}" '
                f'tvg-logo="{ch["tvg_logo"]}" '
                f'group-title="{ch["group"]}" '
                f'tvg-group="{ch["group"]}"'
                f',{ch["name"]}\n'
            )
            f.write(ch['url'] + '\n')

    # Summary
    print(f"Written:  {OUTPUT_FILE}", file=sys.stderr)
    print(f"Total channels: {len(channels)}", file=sys.stderr)
    print("--- Group breakdown ---", file=sys.stderr)
    for g in GROUP_ORDER:
        count = chno_counter.get(g, 0)
        if count:
            print(f"  {g:<18} {count}", file=sys.stderr)

if __name__ == '__main__':
    main()
