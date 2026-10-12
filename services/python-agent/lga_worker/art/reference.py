"""Reference pictures from Wikimedia Commons (public API, no key).

A downloaded picture is only ever described as what it is: where it came from (page URL), its licence as Commons states
it, its hash, and whether its Commons title matches the work asked for. Matching a title is not proof of identity; the
caller records `REFERENCE_FETCHED`, never "verified original".
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lga_worker.art.modes import normalize
from lga_worker.errors import ErrorCode, ToolError

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "LocalGeminiAgent/1.0 (local desktop assistant; art reference fetch; contact: owner of this PC)"
MAX_BYTES = 25_000_000
BAD_WORDS = ("detail", "crop", "poster", "shirt", "map of", "stamp", "cover", "logo", "mural", "street", "graffiti", "replica",
             "copy", "pixel", "lego", "cake", "tattoo", "sculpture", "statue", "photograph of", "museum", "frame", "diagram",
             "wikimedia", "icon", "coat of arms", "book", "page", "text", "sketch", "study", "drawing", "detail of",
             "visitors", "visitor", "people", "crowd", "gallery", "tourists", "tourist", "exhibition", "room", "wall", "selfie",
             "interior", "queue", "viewing", "looking at", "in front of", "installation", "view of")


@dataclass
class FetchedReference:
    path: str
    title: str
    page_url: str
    source_url: str
    license: str
    attribution: str
    sha256: str
    width: int
    height: int
    retrieved_at: str
    title_match: float
    candidates: int
    palette_share: float = 1.0
    rejected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https URL built above
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"cannot reach Wikimedia Commons ({exc}); the machine may be offline",
                        retryable=True) from exc
    if len(data) > MAX_BYTES:
        raise ToolError(ErrorCode.EXECUTION_FAILED, "the picture is larger than 25 MB")
    return data


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _score(title: str, query: str, width: int, height: int, license_name: str, aspect: float | None = None) -> float:
    t = normalize(re.sub(r"\.\w+$", "", title.replace("File:", "")))
    q_tokens = [w for w in normalize(query).split() if len(w) > 2]
    if not q_tokens:
        return 0.0
    hit = sum(1 for w in q_tokens if w in t) / len(q_tokens)
    penalty = 0.35 * sum(1 for w in BAD_WORDS if f" {w} " in f" {t} ")
    size = min(1.0, (width * height) / (1500 * 1100))
    pd = 0.1 if re.search(r"public domain|pd|cc0|cc by(-sa)? [1-4]", license_name.lower()) else 0.0
    art_project = 0.5 if "google art project" in t else 0.0
    shape = 0.0
    if aspect and height:
        off = abs((width / height) / aspect - 1.0)
        shape = 0.4 if off < 0.04 else -0.8 if off > 0.12 else 0.0  # a gallery snapshot is not the picture's own shape
    return hit * 2.0 + size * 0.5 + pd + art_project + shape - penalty


def search(query: str, *, timeout: float = 20.0, limit: int = 15, width: int = 1600,
           aspect: float | None = None) -> list[dict[str, Any]]:
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
              "gsrnamespace": "6", "gsrlimit": str(limit), "prop": "imageinfo",
              "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(width)}
    raw = _get(f"{API}?{urllib.parse.urlencode(params)}", timeout)
    try:
        pages = (json.loads(raw).get("query") or {}).get("pages") or {}
    except ValueError as exc:
        raise ToolError(ErrorCode.EXECUTION_FAILED, "Wikimedia Commons returned something that is not JSON") from exc
    found: list[dict[str, Any]] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [None])[0]
        if not info or not str(info.get("mime", "")).startswith("image/"):
            continue
        meta = info.get("extmetadata") or {}
        lic = _clean((meta.get("LicenseShortName") or {}).get("value", ""))
        found.append({"title": page.get("title", ""), "page_url": info.get("descriptionurl", ""),
                      "url": info.get("thumburl") or info.get("url"), "width": int(info.get("thumbwidth") or info.get("width") or 0),
                      "height": int(info.get("thumbheight") or info.get("height") or 0), "license": lic,
                      "attribution": _clean((meta.get("Artist") or {}).get("value", "")),
                      "score": _score(page.get("title", ""), query, int(info.get("width") or 0), int(info.get("height") or 0), lic, aspect)})
    return sorted(found, key=lambda c: -c["score"])


def fetch(query: str, directory: Path, *, must_include: list[str] | None = None, timeout: float = 25.0,
          aspect: float | None = None, palette: list[str] | None = None, min_palette_share: float = 0.5,
          tries: int = 5) -> FetchedReference:
    """Search Commons for `query` and download the best candidate that (a) has every word of `must_include` in its title,
    (b) is not a tile or crop, and (c) when the work's palette is known, is mostly made of the work's colours: a snapshot
    of visitors in front of the painting is rejected by pixels, not only by its title."""
    from PIL import Image
    import io

    candidates = search(query, timeout=timeout, aspect=aspect)
    needed = [normalize(w) for w in (must_include or []) if w]
    usable = [c for c in candidates if all(w in normalize(c["title"]) for w in needed) and c["url"] and c["score"] > 0.8
              and not re.search(r"-x\d+-y\d+", c["title"])]
    if not usable:
        raise ToolError(ErrorCode.NOT_FOUND, f"no matching reference picture for {query!r} on Wikimedia Commons "
                                             f"({len(candidates)} files found, none whose title matches)")
    rejected: list[str] = []
    for best in usable[:tries]:
        data = _get(best["url"], timeout)
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                width, height = im.size
                share = palette_share(im, palette) if palette else 1.0
        except Exception as exc:  # noqa: BLE001
            rejected.append(f"{best['title']}: unreadable ({exc})")
            continue
        if share < min_palette_share:
            rejected.append(f"{best['title']}: only {share:.0%} of its pixels are the work's colours")
            continue
        digest = hashlib.sha256(data).hexdigest()
        ext = Path(urllib.parse.urlparse(best["url"]).path).suffix.lower() or ".jpg"
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", normalize(query))[:48].strip("-") or "reference"
        path = directory / f"{slug}-{digest[:8]}{ext}"
        path.write_bytes(data)
        return FetchedReference(
            path=str(path), title=best["title"], page_url=best["page_url"], source_url=best["url"],
            license=best["license"] or "unknown", attribution=best["attribution"], sha256=digest, width=width, height=height,
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), title_match=round(best["score"], 2),
            candidates=len(candidates), palette_share=round(share, 2), rejected=rejected)
    raise ToolError(ErrorCode.NOT_FOUND, "no candidate looks like the work: " + "; ".join(rejected[:4]))


def palette_share(image: "Image.Image", palette: list[str], distance: float = 58.0) -> float:
    """Share of the picture's pixels that lie near one of the work's known colours ('#RRGGBB name' entries). A museum
    snapshot of the painting (wall, visitors, frame) scores low; the painting itself scores high."""
    import numpy as np

    colours = []
    for entry in palette:
        m = re.match(r"#([0-9a-fA-F]{6})", entry.strip())
        if m:
            colours.append([int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)])
    if not colours:
        return 1.0
    small = np.asarray(image.convert("RGB").resize((96, 72)), np.float32).reshape(-1, 3)
    pal = np.asarray(colours, np.float32)
    d = np.sqrt(((small[:, None, :] - pal[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
    return float((d < distance).mean())
