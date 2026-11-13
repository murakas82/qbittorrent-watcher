"""Pure release matching and torrent identity helpers."""

import base64
import re

COUNTRY_TAGS = {"us", "uk", "au", "ca", "nz"}


def normalize(text):
    """Lowercase and turn every run of punctuation into one space."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def is_title_extra(word):
    return word in COUNTRY_TAGS or re.fullmatch(r"(19|20)\d\d", word) is not None


def name_matches(show, text):
    """True if `text` (a release title, up to the episode number) is this series.

    Matching is by whole words, ignoring case, punctuation and how the words
    are spaced, so "example show" and "Example-Show" both match "ExampleShow",
    but "You" does not match "Young Sheldon".

    Normally the title has to be the series name and nothing else, apart from
    a leading "The" and a trailing year or country tag ("Lupin 2021",
    "The Office US"). A series marked "part_of_title" matches when its name is
    a run of whole words anywhere in the title, for names typed as the short
    form of a longer title.
    """
    wanted = normalize(show["name"]).split()
    words = normalize(text).split()
    if not wanted or not words:
        return False
    target = "".join(wanted)

    if show.get("part_of_title"):
        for start in range(len(words)):
            joined = ""
            for word in words[start:]:
                joined += word
                if joined == target:
                    return True
                if len(joined) >= len(target):
                    break
        return False

    targets = {target}
    if wanted[0] == "the" and len(wanted) > 1:
        targets.add("".join(wanted[1:]))
    variants = [words]
    if words[0] == "the" and len(words) > 1:
        variants.append(words[1:])
    for variant in variants:
        trimmed = list(variant)
        while True:
            if "".join(trimmed) in targets:
                return True
            if len(trimmed) > 1 and is_title_extra(trimmed[-1]):
                trimmed.pop()
            else:
                break
    return False


def episode_pattern(show):
    """Regex for the show's next episode in a normalized title.

    Matches "s02e06", "s02 e06" and "2x06", but not "s02e060" or "12x06".
    """
    season, episode = show["season"], show["next_episode"]
    return re.compile(
        rf"(?<![a-z0-9])(?:s0*{season} ?e0*{episode}|0*{season}x0*{episode})(?![0-9])"
    )


def find_episode(torrents, show):
    """Return the torrents from the list that are the show's next episode."""
    tag = episode_pattern(show)
    matches = []
    for torrent in torrents:
        title = normalize(torrent["name"])
        found = tag.search(title)
        if found and name_matches(show, title[: found.start()]):
            matches.append(torrent)
    return matches


def magnet_hash(url):
    """The torrent ID a magnet link names, as 40 lowercase hex digits, or None."""
    found = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", url)
    if not found:
        return None
    value = found.group(1)
    if len(value) == 40 and re.fullmatch(r"[0-9a-fA-F]+", value):
        return value.lower()
    if len(value) == 32:  # the same ID written in base32
        try:
            return base64.b32decode(value.upper()).hex()
        except ValueError:
            return None
    return None


def pick_candidates(results, show):
    """Return matching results for the show's next episode, best first."""
    quality = normalize(show.get("quality", ""))
    banned = {normalize(w) for w in show.get("must_not_contain", [])}
    episode_tag = episode_pattern(show)

    candidates = []
    for row in results:
        title = normalize(row.get("fileName", ""))
        url = row.get("fileUrl", "")
        tag = episode_tag.search(title)
        if not tag or not url.startswith(("magnet:", "http://", "https://")):
            continue
        # The show name must come before the episode tag.
        if not name_matches(show, title[: tag.start()]):
            continue
        if quality and quality not in title.split():
            continue
        if banned & set(title.split()):
            continue
        if row.get("nbSeeders", 0) < show.get("min_seeders", 1):
            continue
        candidates.append(row)

    return sorted(candidates, key=lambda r: r.get("nbSeeders", 0), reverse=True)


def pick_best(results, show):
    """Return the best result for the show's next episode, or None."""
    candidates = pick_candidates(results, show)
    return candidates[0] if candidates else None
