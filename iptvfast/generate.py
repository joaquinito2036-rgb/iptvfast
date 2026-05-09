from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "sources.yml"
OUT = ROOT / "output"

USER_AGENT = os.getenv(
    "IPTVFAST_USER_AGENT",
    "IPTVFast/1.0 (+https://github.com/your-user/iptvfast)"
)
CONCURRENCY = int(os.getenv("IPTVFAST_CONCURRENCY", "32"))
TIMEOUT = int(os.getenv("IPTVFAST_TIMEOUT", "25"))
RESOLVE_REDIRECTS = os.getenv("IPTVFAST_RESOLVE_REDIRECTS", "true").lower() == "true"
WRITE_JSON_PLAIN = os.getenv("IPTVFAST_WRITE_JSON_PLAIN", "false").lower() == "true"
MAX_XMLTV_GZ_BYTES = int(os.getenv("IPTVFAST_MAX_XMLTV_GZ_BYTES", str(100 * 1024 * 1024)))
XMLTV_GZIP_LEVEL = int(os.getenv("IPTVFAST_XMLTV_GZIP_LEVEL", "9"))


JMP_RE = re.compile(
    r"https?://(?:jmp2\.uk/(?:plu|rok|plex)-[^ \n\r\t]+|jmp2\.uk/stvp-[^ \n\r\t]+|i\.mjh\.nz/\.r/[^ \n\r\t]+)",
    re.I,
)


@dataclass
class Channel:
    id: str
    name: str
    url: str
    platform: str
    country: str = "all"
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    tvg_name: str = ""
    original_url: str = ""
    license_url: str = ""
    key_system: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    raw_attrs: dict[str, str] = field(default_factory=dict)


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "unknown"


def stable_id(platform: str, country: str, name: str, url: str) -> str:
    src = f"{platform}|{country}|{name}|{url}".encode("utf-8", "ignore")
    return hashlib.sha1(src).hexdigest()[:16]


def parse_extinf_attrs(line: str) -> tuple[dict[str, str], str]:
    # #EXTINF:-1 tvg-id="x" tvg-logo="y",Name
    name = line.split(",", 1)[1].strip() if "," in line else ""
    before_comma = line.split(",", 1)[0]
    attrs = dict(re.findall(r'([A-Za-z0-9_.:-]+)="([^"]*)"', before_comma))
    return attrs, name


def parse_m3u(text: str, platform: str, country: str) -> list[Channel]:
    channels: list[Channel] = []
    current_attrs: dict[str, str] = {}
    current_name = ""
    current_props: dict[str, str] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTINF"):
            current_attrs, current_name = parse_extinf_attrs(line)
            current_props = {}
            continue

        if line.startswith("#KODIPROP:"):
            keyval = line.removeprefix("#KODIPROP:")
            if "=" in keyval:
                k, v = keyval.split("=", 1)
                current_props[k.strip()] = v.strip()
            continue

        if line.startswith("#"):
            continue

        if "://" in line:
            url = line
            name = current_name or current_attrs.get("tvg-name") or Path(urlparse(url).path).stem or url
            ch = Channel(
                id=stable_id(platform, country, name, url),
                name=name,
                url=url,
                original_url=url,
                platform=platform,
                country=(country or current_attrs.get("country") or "all").lower(),
                group=current_attrs.get("group-title", platform),
                logo=current_attrs.get("tvg-logo", ""),
                tvg_id=current_attrs.get("tvg-id", ""),
                tvg_name=current_attrs.get("tvg-name", name),
                raw_attrs=current_attrs.copy(),
            )
            # Preserve common DRM InputStream props already present in M3U
            for k, v in current_props.items():
                lk = k.lower()
                if "license" in lk:
                    ch.license_url = v
                if "drm" in lk or "manifest_type" in lk:
                    ch.raw_attrs[f"kodiprop:{k}"] = v
            channels.append(ch)

            current_attrs = {}
            current_name = ""
            current_props = {}

    return channels


def extract_channels_from_matt_json(obj: Any, platform: str) -> list[Channel]:
    """Best-effort parser for Matt Huisman JSON shapes."""
    channels: list[Channel] = []

    def visit(node: Any, context: dict[str, Any] | None = None):
        context = context or {}
        if isinstance(node, dict):
            url = (
                node.get("url")
                or node.get("stream")
                or node.get("stream_url")
                or node.get("playback_url")
                or node.get("hls")
                or node.get("manifest")
            )
            name = (
                node.get("name")
                or node.get("title")
                or node.get("label")
                or node.get("channel")
                or context.get("name")
            )
            if isinstance(url, str) and "://" in url and name:
                country = str(node.get("country") or node.get("region") or context.get("country") or "all").lower()
                logo = str(node.get("logo") or node.get("logo_url") or node.get("image") or "")
                tvg_id = str(node.get("id") or node.get("tvg_id") or node.get("slug") or "")
                license_url = str(
                    node.get("drm_license")
                    or node.get("license_url")
                    or node.get("license")
                    or node.get("widevine_license")
                    or ""
                )
                key_system = str(node.get("key_system") or node.get("drm") or node.get("drm_type") or "")
                ch = Channel(
                    id=stable_id(platform, country, str(name), url),
                    name=str(name),
                    url=url,
                    original_url=url,
                    platform=platform,
                    country=country,
                    group=str(node.get("group") or node.get("group-title") or platform),
                    logo=logo,
                    tvg_id=tvg_id,
                    tvg_name=str(node.get("tvg_name") or name),
                    license_url=license_url,
                    key_system=key_system,
                    raw_attrs={k: str(v) for k, v in node.items() if isinstance(v, (str, int, float, bool))},
                )
                channels.append(ch)
            for k, v in node.items():
                next_context = context.copy()
                if k in ("country", "region") and isinstance(v, str):
                    next_context["country"] = v
                if k in ("name", "title") and isinstance(v, str):
                    next_context["name"] = v
                visit(v, next_context)
        elif isinstance(node, list):
            for item in node:
                visit(item, context)

    visit(obj)
    return channels


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.read()
        if url.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except gzip.BadGzipFile:
                pass
        return data.decode("utf-8", "replace")


async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        return await resp.read()


async def resolve_url(session: aiohttp.ClientSession, url: str) -> str:
    if not RESOLVE_REDIRECTS or not JMP_RE.search(url):
        return url
    try:
        async with session.head(url, timeout=TIMEOUT, allow_redirects=True) as resp:
            return str(resp.url)
    except Exception:
        try:
            headers = {"Range": "bytes=0-0"}
            async with session.get(url, timeout=TIMEOUT, allow_redirects=True, headers=headers) as resp:
                return str(resp.url)
        except Exception:
            return url


def channel_to_m3u(ch: Channel) -> str:
    attrs = {
        "tvg-id": ch.tvg_id or ch.id,
        "tvg-name": ch.tvg_name or ch.name,
        "tvg-logo": ch.logo,
        "group-title": ch.group or ch.platform,
        "platform": ch.platform,
        "country": ch.country,
    }
    attrs_str = " ".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in attrs.items() if v)
    lines = [f"#EXTINF:-1 {attrs_str},{ch.name}"]

    if ch.license_url:
        lines.append("#KODIPROP:inputstream=inputstream.adaptive")
        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=hls")
        if ch.key_system:
            lines.append(f"#KODIPROP:inputstream.adaptive.license_type={ch.key_system}")
        lines.append(f"#KODIPROP:inputstream.adaptive.license_key={ch.license_url}")

    lines.append(ch.url)
    return "\n".join(lines)


def write_m3u(path: Path, channels: list[Channel], epg_url: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = '#EXTM3U'
    if epg_url:
        header += f' x-tvg-url="{epg_url}"'
    body = "\n".join(channel_to_m3u(c) for c in channels)
    path.write_text(header + "\n" + body + "\n", encoding="utf-8")


def write_json_gz(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(raw)
    if WRITE_JSON_PLAIN:
        path.with_suffix("").write_bytes(raw)


def dedupe(channels: list[Channel]) -> list[Channel]:
    seen = set()
    out = []
    for ch in channels:
        key = (ch.url, ch.tvg_id or ch.name.lower(), ch.platform, ch.country)
        if key in seen:
            continue
        seen.add(key)
        out.append(ch)
    return out


def norm_match_text(value: str) -> str:
    value = (value or "").casefold()
    value = re.sub(r"&amp;", " and ", value)
    value = re.sub(r"[^a-z0-9áéíóúüñçàèìòùäëïöüâêîôû]+", "", value, flags=re.I)
    return value


def xmltv_time_to_dt(value: str):
    if not value:
        return None
    m = re.match(r"(\d{14})(?:\s*([+-]\d{4}))?", value)
    if not m:
        return None
    raw, tz = m.groups()
    try:
        if tz:
            return datetime.strptime(raw + tz, "%Y%m%d%H%M%S%z")
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def channel_match_tokens(channels: list[Channel]) -> set[str]:
    tokens: set[str] = set()
    for ch in channels:
        for value in (ch.tvg_id, ch.tvg_name, ch.name):
            n = norm_match_text(value)
            if len(n) >= 3:
                tokens.add(n)
    return tokens


def channel_matches_xmltv(channel_id: str, display_names: list[str], tokens: set[str]) -> bool:
    candidates = [channel_id, *display_names]
    normalized = [norm_match_text(x) for x in candidates if x]
    for n in normalized:
        if not n:
            continue
        if n in tokens:
            return True
        # Coincidencia flexible por nombre: sirve para pequeñas diferencias de mayúsculas,
        # espacios, guiones, acentos o sufijos.
        for t in tokens:
            if len(t) >= 5 and (t in n or n in t):
                return True
    return False


def clone_element(elem: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(elem, encoding="utf-8"))


def write_filtered_xmltv_gz(
    source_bytes: bytes,
    out_path: Path,
    channels: list[Channel],
    max_gz_bytes: int = MAX_XMLTV_GZ_BYTES,
    max_days: int = 7,
) -> dict[str, object]:
    """Filter XMLTV to generated channel names and gzip with max compression.

    Strategy:
    1. Match XMLTV <channel> by id/display-name against generated M3U channel names/tvg ids.
    2. Keep only <programme> whose channel survived.
    3. Start with up to 7 days, then reduce days if gzip is still over max_gz_bytes.
    """
    if source_bytes[:2] == b"\x1f\x8b":
        xml_bytes = gzip.decompress(source_bytes)
    else:
        xml_bytes = source_bytes

    tokens = channel_match_tokens(channels)
    best_meta: dict[str, object] = {}
    best_payload = b""

    now = datetime.now(timezone.utc)

    # Try requested days first, then reduce to satisfy <=100 MB.
    for days in range(int(max_days), 0, -1):
        cutoff = now.timestamp() + days * 86400

        root_out = ET.Element("tv", {
            "generator-info-name": "IPTVFast filtered XMLTV",
            "source-info-name": "epgshare01 filtered by generated channel names",
        })

        kept_ids: set[str] = set()
        kept_channels = 0
        kept_programmes = 0

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            tmp.write(xml_bytes)
            tmp_path = tmp.name

        try:
            # Pass 1: channels
            for event, elem in ET.iterparse(tmp_path, events=("end",)):
                if elem.tag == "channel":
                    cid = elem.attrib.get("id", "")
                    names = [dn.text or "" for dn in elem.findall("display-name")]
                    if channel_matches_xmltv(cid, names, tokens):
                        kept_ids.add(cid)
                        root_out.append(clone_element(elem))
                        kept_channels += 1
                    elem.clear()

            # Pass 2: programmes
            for event, elem in ET.iterparse(tmp_path, events=("end",)):
                if elem.tag == "programme":
                    cid = elem.attrib.get("channel", "")
                    if cid in kept_ids:
                        start = xmltv_time_to_dt(elem.attrib.get("start", ""))
                        if start is None or start.timestamp() <= cutoff:
                            root_out.append(clone_element(elem))
                            kept_programmes += 1
                    elem.clear()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        xml_out = ET.tostring(root_out, encoding="utf-8", xml_declaration=True)
        gz_payload = gzip.compress(xml_out, compresslevel=XMLTV_GZIP_LEVEL, mtime=0)

        best_meta = {
            "xmltv_file": out_path.name,
            "xmltv_max_days_requested": max_days,
            "xmltv_days_written": days,
            "xmltv_channels_written": kept_channels,
            "xmltv_programmes_written": kept_programmes,
            "xmltv_gz_bytes": len(gz_payload),
            "xmltv_gz_limit_bytes": max_gz_bytes,
            "xmltv_gzip_level": XMLTV_GZIP_LEVEL,
            "xmltv_filtered_by_channel_name": True,
        }
        best_payload = gz_payload

        if len(gz_payload) <= max_gz_bytes:
            break

    out_path.write_bytes(best_payload)
    best_meta["xmltv_under_limit"] = len(best_payload) <= max_gz_bytes
    return best_meta



async def main() -> int:
    OUT.mkdir(exist_ok=True)
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=TIMEOUT * 4)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        sources = list(cfg.get("m3u_sources", []))

        # Generate country/template URLs, e.g. Whale TV Plus and LG Channels
        for item in cfg.get("generated_sources", {}).values():
            slugs = item.get("slugs", {}) or {}
            for country in item.get("countries", []):
                sources.append({
                    "platform": item["platform"],
                    "country": country,
                    "url": item["template"].format(country=country, slug=slugs.get(country, country)),
                })

        results: list[Channel] = []
        errors: list[dict[str, str]] = []

        async def process_m3u(src: dict[str, str]):
            async with sem:
                try:
                    text = await fetch_text(session, src["url"])
                    parsed = parse_m3u(text, src.get("platform", "unknown"), src.get("country", "all"))
                    for ch in parsed:
                        resolved = await resolve_url(session, ch.url)
                        ch.original_url = ch.url
                        ch.url = resolved
                    results.extend(parsed)
                except Exception as e:
                    errors.append({"url": src.get("url", ""), "platform": src.get("platform", ""), "error": repr(e)})

        async def process_matt(src: dict[str, str]):
            async with sem:
                try:
                    text = await fetch_text(session, src["url"])
                    obj = json.loads(text)
                    parsed = extract_channels_from_matt_json(obj, src.get("platform", "matt"))
                    for ch in parsed:
                        resolved = await resolve_url(session, ch.url)
                        ch.original_url = ch.url
                        ch.url = resolved
                    results.extend(parsed)
                except Exception as e:
                    errors.append({"url": src.get("url", ""), "platform": src.get("platform", ""), "error": repr(e)})

        await asyncio.gather(*(process_m3u(s) for s in sources))
        await asyncio.gather(*(process_matt(s) for s in cfg.get("matt_huisman_json_gz", [])))

        channels = dedupe(results)
        channels.sort(key=lambda c: (c.platform, c.country, c.name.lower()))

        # EPG XMLTV: filter by generated channel names and gzip with max compression.
        # Output name requested: all.xml.gz, max 100 MB by default.
        epg_url = cfg.get("epg", {}).get("url")
        xmltv_meta: dict[str, object] = {}
        local_epg_ref = "all.xml.gz"
        if epg_url:
            try:
                epg_bytes = await fetch_bytes(session, epg_url)
                xmltv_meta = write_filtered_xmltv_gz(
                    epg_bytes,
                    OUT / local_epg_ref,
                    channels,
                    max_gz_bytes=MAX_XMLTV_GZ_BYTES,
                    max_days=int(cfg.get("epg", {}).get("days", 7)),
                )
            except Exception as e:
                errors.append({"url": epg_url, "platform": "xmltv", "error": repr(e)})

        write_m3u(OUT / "all.m3u", channels, local_epg_ref)

        # Platform and country outputs
        by_platform: dict[str, list[Channel]] = {}
        by_platform_country: dict[tuple[str, str], list[Channel]] = {}

        for ch in channels:
            by_platform.setdefault(slugify(ch.platform), []).append(ch)
            by_platform_country.setdefault((slugify(ch.platform), slugify(ch.country)), []).append(ch)

        for platform, items in by_platform.items():
            write_m3u(OUT / f"{platform}_all.m3u", items, local_epg_ref)

        for (platform, country), items in by_platform_country.items():
            if country and country != "all":
                write_m3u(OUT / f"{platform}_{country}.m3u", items, local_epg_ref)

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "channel_count": len(channels),
            "channels": [asdict(c) for c in channels],
            "epg": {
                "source": epg_url,
                "local": local_epg_ref,
                "days": cfg.get("epg", {}).get("days", 7),
                **xmltv_meta,
            },
        }
        write_json_gz(OUT / "all.json.gz", payload)

        manifest = {
            "generated_at": payload["generated_at"],
            "files": sorted(str(p.relative_to(OUT)) for p in OUT.glob("*") if p.is_file()),
            "channel_count": len(channels),
            "errors_count": len(errors),
        }
        (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        summary = {
            "generated_at": payload["generated_at"],
            "channel_count": len(channels),
            "platform_count": len(by_platform),
            "xmltv": xmltv_meta,
            "errors": errors[:200],
        }
        (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if channels else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
