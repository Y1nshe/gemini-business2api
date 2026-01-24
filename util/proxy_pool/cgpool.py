#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml


DEFAULT_GROUP = "POOL_ACTIVE"
DEFAULT_BASE = "http://127.0.0.1:9090"
DEFAULT_STATE = os.path.join(os.path.dirname(__file__), "state", "bans.json")
DEFAULT_PROVIDER_OUT = os.path.join(os.path.dirname(__file__), "providers", "chromego.yaml")
DEFAULT_CHROMEGO_REPO = "bannedbook/fanqiang"
DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(__file__), "state", "cache")
DEFAULT_RELEASE_SCAN_LIMIT = 30
DEFAULT_PROBE_URLS = [
    # Small and stable endpoints.
    "https://www.gstatic.com/generate_204",
    # User requested.
    "https://auth.business.gemini.google/login?continueUrl=https://business.gemini.google/",
    # Another small HTTPS endpoint.
    "https://www.cloudflare.com/cdn-cgi/trace",
]

CHROMEGO_ASSET_PREFER = [
    # Prefer tarballs so we can parse with Python stdlib (tarfile) on any OS.
    "FirefoxFqLinux.tar.gz",
    "ChromeGoMac.tar.gz",
    # Fallback to 7z (requires `7z` command installed).
    "ChromeGo.7z",
    "EdgeGo.7z",
    "FirefoxFQ.7z",
]
CHROMEGO_ASSET_HINT_RE = re.compile(r"(ChromeGo|EdgeGo|FirefoxFqLinux).*\.(7z|tar\.gz)$", re.IGNORECASE)
IP_UPDATE_SCRIPT_RE = re.compile(r"/clash\.meta/ip_Update/ip_(\d+)\.(sh|bat|cmd)$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _now_ts() -> int:
    return int(time.time())


def load_bans(path: str) -> Dict[str, int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            bans = json.load(f)
    except FileNotFoundError:
        return {}

    if not isinstance(bans, dict):
        raise SystemExit(f"invalid bans file (expected object): {path}")

    now = _now_ts()
    out: Dict[str, int] = {}
    for k, v in bans.items():
        try:
            exp = int(v)
        except Exception:
            continue
        if exp > now:
            out[str(k)] = exp
    return out


def save_bans(path: str, bans: Dict[str, int]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bans, f, ensure_ascii=True, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


@dataclass(frozen=True)
class ClashApi:
    base: str
    secret: str

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = self.base.rstrip("/") + path
        data = None
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as ex:
            raw = ex.read().decode("utf-8", errors="replace") if ex.fp else ""
            raise SystemExit(f"clash api http error {ex.code} {ex.reason}: {raw}") from ex
        except Exception as ex:
            raise SystemExit(f"clash api request failed: {method} {url}: {ex}") from ex

        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def get_proxies(self) -> dict:
        data = self._req("GET", "/proxies")
        if not isinstance(data, dict):
            raise SystemExit("unexpected /proxies response")
        return data

    def select_group(self, group: str, proxy_name: str) -> None:
        g = urllib.parse.quote(group, safe="")
        self._req("PUT", f"/proxies/{g}", {"name": proxy_name})


def _resolve_chromego_config(chromego_config: Optional[str], chromego_dir: Optional[str]) -> str:
    if chromego_config:
        return chromego_config

    if not chromego_dir:
        raise SystemExit("need --chromego-config or --chromego-dir")

    # Allow passing either the extracted root or the ChromeGo folder itself.
    candidates = [
        os.path.join(chromego_dir, "clash.meta", "config.yaml"),
        os.path.join(chromego_dir, "ChromeGo", "clash.meta", "config.yaml"),
        os.path.join(chromego_dir, "EdgeGo", "clash.meta", "config.yaml"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p

    raise SystemExit(
        "cannot find ChromeGo config.yaml; tried:\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


def export_provider(chromego_yaml_path: str, out_path: str, strip_name: bool = True) -> int:
    with open(chromego_yaml_path, "r", encoding="utf-8", errors="replace") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise SystemExit(f"unexpected YAML root (expected mapping): {chromego_yaml_path}")

    proxies = data.get("proxies")
    if not isinstance(proxies, list):
        raise SystemExit(f"no proxies list found in: {chromego_yaml_path}")

    cleaned: List[dict] = []
    seen: Dict[str, int] = {}
    for i, p in enumerate(proxies):
        if not isinstance(p, dict):
            continue
        # ChromeGo stable-update configs often set `global-client-fingerprint` at
        # the top-level. Since proxy-providers only contain proxies, keep a
        # reasonable default for REALITY nodes here so they can work standalone.
        if isinstance(p.get("reality-opts"), dict) and "client-fingerprint" not in p:
            p = dict(p)
            p["client-fingerprint"] = "chrome"
        name = p.get("name")
        if isinstance(name, str) and strip_name:
            name = name.rstrip()
            p = dict(p)
            p["name"] = name
        if not isinstance(name, str) or not name:
            continue
        if name in seen:
            seen[name] += 1
            p = dict(p)
            p["name"] = f"{name}__dup{seen[name]}"
        else:
            seen[name] = 0
        cleaned.append(p)

    out = {"proxies": cleaned}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            out,
            f,
            sort_keys=False,
            allow_unicode=False,
            default_flow_style=False,
            width=120,
        )

    return len(cleaned)


def _http_get_text(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "cgpool/1.0 (+https://local)",
            "Accept": "*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _http_get_json(url: str, timeout: int) -> Any:
    txt = _http_get_text(url, timeout=timeout)
    try:
        return json.loads(txt)
    except Exception as ex:
        raise SystemExit(f"invalid json from {url}: {ex}") from ex


def _parse_github_ts(ts: Optional[str]) -> int:
    if not ts:
        return 0
    try:
        # Example: 2026-01-20T04:24:57Z
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


def github_get_release_by_tag(repo: str, tag: str, timeout: int) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    data = _http_get_json(url, timeout=timeout)
    if not isinstance(data, dict):
        raise SystemExit("unexpected github release response")
    return data


def github_list_releases(repo: str, limit: int, timeout: int) -> List[dict]:
    per_page = max(1, min(int(limit), 100))
    url = f"https://api.github.com/repos/{repo}/releases?per_page={per_page}"
    data = _http_get_json(url, timeout=timeout)
    if not isinstance(data, list):
        raise SystemExit("unexpected github releases list response")
    out: List[dict] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
    return out


def is_chromego_release(release: dict) -> bool:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return False
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if isinstance(name, str) and CHROMEGO_ASSET_HINT_RE.search(name):
            return True
    return False


def pick_asset(release: dict, preferred: Optional[str] = None) -> dict:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise SystemExit("release assets missing")

    by_name: Dict[str, dict] = {}
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if isinstance(name, str):
            by_name[name] = a

    if preferred:
        a = by_name.get(preferred)
        if not a:
            raise SystemExit(f"asset not found in release: {preferred}")
        return a

    for name in CHROMEGO_ASSET_PREFER:
        if name in by_name:
            return by_name[name]

    # Last resort: pick the first asset that looks like a Chromego/ChromeGo bundle.
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = a.get("name")
        if isinstance(name, str) and CHROMEGO_ASSET_HINT_RE.search(name):
            return a

    raise SystemExit("no suitable Chromego asset found in the selected release")


def download_to_file(url: str, out_path: str, timeout: int) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "cgpool/1.0 (+https://local)",
            "Accept": "*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, out_path)


def ensure_release_asset_cached(
    repo: str,
    release: dict,
    asset: dict,
    cache_dir: str,
    timeout: int,
    force: bool,
) -> str:
    name = asset.get("name")
    url = asset.get("browser_download_url")
    size = asset.get("size")
    asset_id = asset.get("id")
    if not isinstance(name, str) or not isinstance(url, str):
        raise SystemExit("invalid asset object (missing name/url)")

    out_path = os.path.join(cache_dir, name)
    meta_path = out_path + ".meta.json"

    if not force and os.path.isfile(out_path) and os.path.isfile(meta_path):
        try:
            meta = json.load(open(meta_path, "r", encoding="utf-8"))
        except Exception:
            meta = None
        if isinstance(meta, dict):
            cached = meta.get("asset", {})
            if (
                isinstance(cached, dict)
                and cached.get("id") == asset_id
                and cached.get("size") == size
                and (size is None or os.path.getsize(out_path) == size)
            ):
                return out_path

    download_to_file(url, out_path, timeout=timeout)

    meta = {
        "repo": repo,
        "release": {
            "id": release.get("id"),
            "tag_name": release.get("tag_name"),
            "name": release.get("name"),
            "published_at": release.get("published_at"),
        },
        "asset": {"id": asset_id, "name": name, "size": size, "url": url, "updated_at": asset.get("updated_at")},
    }
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=True, indent=2)
        f.write("\n")

    return out_path


def _extract_urls(text: str) -> List[str]:
    found: List[str] = []
    seen = set()
    for u in URL_RE.findall(text):
        u = u.strip().strip("\"'").rstrip(");")
        if not u or u in seen:
            continue
        seen.add(u)
        found.append(u)
    return found


def discover_update_sources_from_tar_gz(path: str) -> Dict[int, List[str]]:
    sources: Dict[int, List[str]] = {}
    with tarfile.open(path, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name.replace("\\", "/")
            mm = IP_UPDATE_SCRIPT_RE.search(name)
            if not mm:
                continue
            ip = int(mm.group(1))
            f = tf.extractfile(m)
            if not f:
                continue
            txt = f.read().decode("utf-8", errors="replace")
            urls = _extract_urls(txt)
            if not urls:
                continue
            if ip not in sources:
                sources[ip] = []
            for u in urls:
                if u not in sources[ip]:
                    sources[ip].append(u)
    return sources


def discover_update_sources_from_7z(path: str) -> Dict[int, List[str]]:
    # Only used as a fallback if tar.gz assets are not available.
    if not shutil.which("7z"):
        raise SystemExit("need `7z` to parse .7z assets; please install p7zip/7zip or use a .tar.gz asset")
    # List files.
    p = subprocess.run(["7z", "l", path], capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"7z list failed: {p.stderr.strip()}")
    names = []
    for line in p.stdout.splitlines():
        # Rough parse: last column is filename.
        parts = line.split()
        if len(parts) < 6:
            continue
        fn = parts[-1].replace("\\", "/")
        if IP_UPDATE_SCRIPT_RE.search(fn):
            names.append(fn)

    sources: Dict[int, List[str]] = {}
    for fn in names:
        mm = IP_UPDATE_SCRIPT_RE.search(fn)
        if not mm:
            continue
        ip = int(mm.group(1))
        p2 = subprocess.run(["7z", "x", "-so", path, fn], capture_output=True)
        if p2.returncode != 0:
            continue
        txt = p2.stdout.decode("utf-8", errors="replace")
        urls = _extract_urls(txt)
        if not urls:
            continue
        if ip not in sources:
            sources[ip] = []
        for u in urls:
            if u not in sources[ip]:
                sources[ip].append(u)
    return sources


def discover_update_sources_from_asset(asset_path: str) -> Dict[int, List[str]]:
    lower = asset_path.lower()
    if lower.endswith(".tar.gz"):
        return discover_update_sources_from_tar_gz(asset_path)
    if lower.endswith(".7z"):
        return discover_update_sources_from_7z(asset_path)
    raise SystemExit(f"unsupported asset type: {asset_path}")


def find_latest_chromego_release(repo: str, timeout: int, limit: int) -> dict:
    releases = github_list_releases(repo, limit=limit, timeout=timeout)
    candidates = [r for r in releases if is_chromego_release(r)]
    if not candidates:
        raise SystemExit("cannot find a Chromego/ChromeGo release in github releases list")
    candidates.sort(key=lambda r: _parse_github_ts(r.get("published_at") or r.get("created_at")), reverse=True)
    return candidates[0]


def fetch_proxies_yaml_from_urls(urls: List[str], timeout: int) -> Tuple[str, str]:
    last_err: Optional[str] = None
    for url in urls:
        try:
            txt = _http_get_text(url, timeout=timeout)
            data = yaml.safe_load(txt)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                return url, txt
            last_err = f"bad yaml or missing proxies: {url}"
        except Exception as ex:
            last_err = f"{url}: {ex}"
            continue
    raise SystemExit(f"failed to fetch a usable config; last error: {last_err}")


def fetch_chromego_config(
    repo: str,
    tag: Optional[str],
    ip_index: int,
    timeout: int,
    cache_dir: str,
    asset_name: Optional[str],
    force: bool,
    scan_limit: int,
) -> Tuple[str, str, dict]:
    # 1) Select the release (by explicit tag, or heuristically pick the latest Chromego release).
    if tag:
        release = github_get_release_by_tag(repo, tag=tag, timeout=timeout)
    else:
        release = find_latest_chromego_release(repo, timeout=timeout, limit=scan_limit)

    # 2) Pick and download a suitable asset from that release.
    asset = pick_asset(release, preferred=asset_name)
    asset_path = ensure_release_asset_cached(repo, release, asset, cache_dir=cache_dir, timeout=timeout, force=force)

    # 3) Discover ip_*.{sh,bat,cmd} URLs inside the asset, then fetch the config.
    sources = discover_update_sources_from_asset(asset_path)
    urls = sources.get(ip_index)
    if not urls:
        raise SystemExit(f"no update URLs found for ip{ip_index} in asset {asset.get('name')}")

    used_url, cfg_txt = fetch_proxies_yaml_from_urls(urls, timeout=timeout)
    meta = {
        "repo": repo,
        "tag": release.get("tag_name"),
        "release_name": release.get("name"),
        "asset": asset.get("name"),
        "ip": ip_index,
        "discovered_urls": urls,
        "used_url": used_url,
    }
    return used_url, cfg_txt, meta


def _pick_next(candidates: List[str], current: Optional[str], bans: Dict[str, int]) -> Optional[str]:
    if not candidates:
        return None

    skip = {"DIRECT", "REJECT"}
    try:
        start = (candidates.index(current) + 1) % len(candidates) if current else 0
    except ValueError:
        start = 0

    for i in range(len(candidates)):
        cand = candidates[(start + i) % len(candidates)]
        if current and cand == current:
            continue
        if cand in skip:
            continue
        if cand in bans:
            continue
        return cand
    return None


def _get_group(proxies_json: dict, group: str) -> Tuple[Optional[str], List[str]]:
    proxies = proxies_json.get("proxies", {})
    if not isinstance(proxies, dict):
        raise SystemExit("unexpected /proxies format: missing proxies object")
    g = proxies.get(group)
    if not isinstance(g, dict):
        available = [k for k, v in proxies.items() if isinstance(v, dict) and "all" in v]
        raise SystemExit(
            f"group not found: {group}\n"
            + ("available groups:\n" + "\n".join(f"  - {n}" for n in available) if available else "")
        )
    now = g.get("now")
    all_list = g.get("all", [])
    if now is not None and not isinstance(now, str):
        now = None
    if not isinstance(all_list, list) or not all(isinstance(x, str) for x in all_list):
        raise SystemExit(f"unexpected group format for {group}: missing all[]")
    return now, list(all_list)


def cmd_status(args: argparse.Namespace) -> None:
    bans = load_bans(args.state)
    api = ClashApi(args.base, args.secret)
    proxies_json = api.get_proxies()
    now, all_list = _get_group(proxies_json, args.group)
    print(f"group={args.group} now={now} candidates={len(all_list)} bans={len(bans)}")


def cmd_list(args: argparse.Namespace) -> None:
    bans = load_bans(args.state)
    api = ClashApi(args.base, args.secret)
    proxies_json = api.get_proxies()
    now, all_list = _get_group(proxies_json, args.group)

    print(f"group={args.group} now={now}")
    now_ts = _now_ts()
    for name in all_list:
        exp = bans.get(name)
        if exp:
            left = exp - now_ts
            if left < 0:
                left = 0
            print(f"- {name}  [cooldown {left}s]")
        else:
            print(f"- {name}")


def cmd_ban(args: argparse.Namespace) -> None:
    bans = load_bans(args.state)
    api = ClashApi(args.base, args.secret)
    proxies_json = api.get_proxies()
    now, _all_list = _get_group(proxies_json, args.group)

    target = args.name or now
    if not target:
        raise SystemExit("no current proxy to ban (and no --name provided)")

    bans[target] = _now_ts() + int(args.cooldown)
    save_bans(args.state, bans)
    print(f"banned {target} for {int(args.cooldown)}s")


def cmd_unban(args: argparse.Namespace) -> None:
    bans = load_bans(args.state)
    if args.name not in bans:
        print(f"not banned: {args.name}")
        return
    bans.pop(args.name, None)
    save_bans(args.state, bans)
    print(f"unbanned {args.name}")


def cmd_switch(args: argparse.Namespace) -> None:
    cooldown = int(args.cooldown)
    bans = load_bans(args.state)

    api = ClashApi(args.base, args.secret)
    proxies_json = api.get_proxies()
    now, all_list = _get_group(proxies_json, args.group)

    if now:
        bans[now] = _now_ts() + cooldown

    nxt = _pick_next(all_list, now, bans)
    if not nxt:
        save_bans(args.state, bans)
        raise SystemExit("no available proxy (all candidates are in cooldown or skipped)")

    api.select_group(args.group, nxt)
    save_bans(args.state, bans)
    print(f"switched {now} -> {nxt} (cooldown {cooldown}s)")


def cmd_select(args: argparse.Namespace) -> None:
    cooldown = int(args.cooldown)
    bans = load_bans(args.state)

    api = ClashApi(args.base, args.secret)
    proxies_json = api.get_proxies()
    now, all_list = _get_group(proxies_json, args.group)

    target = str(args.name)
    if target not in all_list:
        raise SystemExit(f"proxy not in group {args.group}: {target}")

    if target in bans and not args.force:
        left = bans[target] - _now_ts()
        if left < 0:
            left = 0
        raise SystemExit(f"proxy is in cooldown for {left}s: {target} (use --force to override)")

    if cooldown > 0 and now:
        bans[now] = _now_ts() + cooldown

    api.select_group(args.group, target)
    save_bans(args.state, bans)
    print(f"selected {now} -> {target} (cooldown {cooldown}s)")


def cmd_export(args: argparse.Namespace) -> None:
    src = _resolve_chromego_config(args.chromego_config, args.chromego_dir)
    out = args.out
    n = export_provider(src, out, strip_name=not args.no_strip_name)
    print(f"exported {n} proxies from {src}")
    print(f"wrote provider: {out}")


def cmd_fetch(args: argparse.Namespace) -> None:
    used_url, txt, meta = fetch_chromego_config(
        repo=str(args.repo),
        tag=args.tag,
        ip_index=int(args.ip),
        timeout=int(args.timeout),
        cache_dir=str(args.cache_dir),
        asset_name=args.asset,
        force=bool(args.force),
        scan_limit=int(args.scan_limit),
    )

    raw_out = args.raw_out
    if raw_out:
        os.makedirs(os.path.dirname(raw_out), exist_ok=True)
        with open(raw_out, "w", encoding="utf-8") as f:
            f.write(txt)
        src_path = raw_out
    else:
        # Keep it simple: write to a temp file for export_provider().
        tmpdir = os.path.join(os.path.dirname(__file__), "state")
        os.makedirs(tmpdir, exist_ok=True)
        fd, src_path = tempfile.mkstemp(prefix=f"chromego-ip{int(args.ip)}-", suffix=".yaml", dir=tmpdir)
        os.close(fd)
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(txt)

    n = export_provider(src_path, args.out, strip_name=True)
    print(f"repo: {meta.get('repo')}")
    print(f"release tag: {meta.get('tag')}")
    if meta.get("asset"):
        print(f"asset: {meta.get('asset')}")
    print(f"ip: {meta.get('ip')}")
    print(f"fetched from: {used_url}")
    print(f"exported {n} proxies -> {args.out}")
    if raw_out:
        print(f"saved raw config -> {raw_out}")
    else:
        print(f"saved temp raw config -> {src_path}")


def _curl_head(proxy_url: str, url: str, connect_timeout: int, max_time: int) -> Tuple[Optional[int], Optional[float]]:
    # Uses curl for robust HTTP proxy + TLS CONNECT handling without extra deps.
    cmd = [
        "curl",
        "-x",
        proxy_url,
        "-I",
        "-sS",
        "-o",
        "/dev/null",
        "--connect-timeout",
        str(connect_timeout),
        "--max-time",
        str(max_time),
        "-w",
        "%{http_code} %{time_total}",
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return None, None
    try:
        code_s, t_s = p.stdout.strip().split(" ", 1)
        return int(code_s), float(t_s)
    except Exception:
        return None, None


def _curl_get(proxy_url: str, url: str, connect_timeout: int, max_time: int) -> Optional[str]:
    cmd = [
        "curl",
        "-x",
        proxy_url,
        "-sS",
        "--connect-timeout",
        str(connect_timeout),
        "--max-time",
        str(max_time),
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return p.stdout


def cmd_probe(args: argparse.Namespace) -> None:
    api = ClashApi(args.base, args.secret)
    proxies_json = api.get_proxies()
    _now, all_list = _get_group(proxies_json, args.group)

    # Keep behavior predictable: iterate candidates in the order exposed by the core.
    candidates = []
    for name in all_list:
        if name in {"DIRECT", "REJECT"}:
            continue
        candidates.append(name)

    # Optional filter: only test nodes that Clash/Mihomo currently considers "alive".
    if args.only_alive:
        proxies = proxies_json.get("proxies", {})
        alive = []
        for name in candidates:
            obj = proxies.get(name)
            if isinstance(obj, dict) and obj.get("alive") is True:
                alive.append(name)
        candidates = alive

    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]

    urls = args.url or list(DEFAULT_PROBE_URLS)
    results = []
    ok_cnt = 0
    stop_after_ok = int(args.stop_after_ok)
    for name in candidates:
        api.select_group(args.group, name)
        tests = {}
        ok_all = True
        for u in urls:
            code, t = _curl_head(args.proxy, u, connect_timeout=args.connect_timeout, max_time=args.max_time)
            tests[u] = {"code": code, "time": t}
            if code is None or not (200 <= code < 400):
                ok_all = False
        ip = None
        if ok_all and args.ipify:
            out = _curl_get(args.proxy, "https://api.ipify.org", connect_timeout=args.connect_timeout, max_time=args.max_time)
            if out:
                ip = out.strip().splitlines()[0]

        results.append({"name": name, "ok": ok_all, "tests": tests, "ipify": ip})
        if ok_all:
            ok_cnt += 1
            if stop_after_ok > 0 and ok_cnt >= stop_after_ok:
                break

    ts = int(time.time())
    report = {
        "ts": ts,
        "group": args.group,
        "base": args.base,
        "proxy": args.proxy,
        "urls": urls,
        "connect_timeout": args.connect_timeout,
        "max_time": args.max_time,
        "results": results,
    }

    out_path = args.out or os.path.join(os.path.dirname(__file__), "state", f"probe-{ts}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=2)
        f.write("\n")

    print(f"tested={len(results)} candidates={len(candidates)} ok={ok_cnt} urls={len(urls)}")
    print(f"report={out_path}")
    for r in results:
        if args.only_ok and not r["ok"]:
            continue
        parts = []
        for u in urls:
            t = r["tests"][u]
            parts.append(f"{t['code']} ({t['time']})")
        ip_s = f" ip={r['ipify']}" if r.get("ipify") else ""
        print(f"- {r['name']}: " + " | ".join(parts) + ip_s)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cgpool",
        description="Export ChromeGo/Chromego nodes and manually switch a Clash/Mihomo select group with cooldown.",
    )
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"external-controller base URL (default: {DEFAULT_BASE})")
    ap.add_argument("--secret", default=os.environ.get("CLASH_SECRET", ""), help="external-controller secret (or env CLASH_SECRET)")
    ap.add_argument("--group", default=DEFAULT_GROUP, help=f"select group name (default: {DEFAULT_GROUP})")
    ap.add_argument("--state", default=DEFAULT_STATE, help=f"cooldown state file (default: {DEFAULT_STATE})")

    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="show current selection and ban stats")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="list candidates with cooldown status")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("switch", help="switch to next non-banned proxy; ban current for cooldown seconds")
    p.add_argument("--cooldown", type=int, default=1800, help="cooldown seconds for the proxy being switched off (default: 1800)")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("select", help="select a specific proxy in the group; optionally ban current for cooldown seconds")
    p.add_argument("--name", required=True, help="proxy name to select")
    p.add_argument("--cooldown", type=int, default=1800, help="cooldown seconds for the proxy being switched off (default: 1800)")
    p.add_argument("--force", action="store_true", help="allow selecting a proxy even if it is in cooldown")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("ban", help="ban a proxy (default: current selection) for cooldown seconds")
    p.add_argument("--cooldown", type=int, default=1800, help="cooldown seconds (default: 1800)")
    p.add_argument("--name", help="proxy name to ban (default: current)")
    p.set_defaults(func=cmd_ban)

    p = sub.add_parser("unban", help="remove a proxy from cooldown list")
    p.add_argument("--name", required=True, help="proxy name to unban")
    p.set_defaults(func=cmd_unban)

    p = sub.add_parser("export", help="export proxies from ChromeGo clash.meta/config.yaml to a provider file")
    p.add_argument("--chromego-config", help="path to ChromeGo config.yaml (clash.meta/config.yaml)")
    p.add_argument("--chromego-dir", help="path to ChromeGo folder (containing clash.meta/config.yaml)")
    p.add_argument("--out", default=DEFAULT_PROVIDER_OUT, help=f"provider output path (default: {DEFAULT_PROVIDER_OUT})")
    p.add_argument("--no-strip-name", action="store_true", help="do not rstrip() proxy names when exporting")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("fetch", help="fetch Chromego 'stable update' config by discovering URLs from the latest Chromego release asset")
    p.add_argument("--repo", default=DEFAULT_CHROMEGO_REPO, help=f"github repo (default: {DEFAULT_CHROMEGO_REPO})")
    p.add_argument("--tag", help="force a specific release tag (default: auto-detect latest Chromego release)")
    p.add_argument("--asset", help="force a specific asset name in that release (default: auto pick)")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help=f"cache dir for downloaded release assets (default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--force", action="store_true", help="force re-download release asset even if cached")
    p.add_argument("--scan-limit", type=int, default=DEFAULT_RELEASE_SCAN_LIMIT, help=f"how many github releases to scan when auto-detecting (default: {DEFAULT_RELEASE_SCAN_LIMIT})")
    p.add_argument("--ip", type=int, default=1, help="which ip_Update to use (typically 1..6, default: 1)")
    p.add_argument("--timeout", type=int, default=20, help="network timeout seconds (default: 20)")
    p.add_argument("--out", default=DEFAULT_PROVIDER_OUT, help=f"provider output path (default: {DEFAULT_PROVIDER_OUT})")
    p.add_argument("--raw-out", help="optional path to save the fetched raw config file (recommended for debugging)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("probe", help="test candidate nodes by fetching URLs through the local proxy")
    p.add_argument("--proxy", default="http://127.0.0.1:7893", help="local proxy URL (default: http://127.0.0.1:7893)")
    p.add_argument(
        "--url",
        action="append",
        default=None,
        help="test URL (repeatable). If omitted, uses a small default set (gstatic_204 + gemini business login + cloudflare trace).",
    )
    p.add_argument("--connect-timeout", type=int, default=4, help="curl connect timeout seconds (default: 4)")
    p.add_argument("--max-time", type=int, default=10, help="curl max time seconds per URL (default: 10)")
    p.add_argument("--limit", type=int, default=0, help="only test first N candidates (default: 0=all)")
    p.add_argument("--only-alive", action="store_true", help="only test nodes currently marked alive by mihomo health-check")
    p.add_argument("--ipify", action="store_true", help="for OK nodes, also fetch https://api.ipify.org and record IP")
    p.add_argument("--stop-after-ok", type=int, default=0, help="stop after finding N OK nodes (default: 0=do not stop early)")
    p.add_argument("--only-ok", action="store_true", help="only print OK nodes")
    p.add_argument("--out", help="write JSON report to this path (default: ./state/probe-<ts>.json)")
    p.set_defaults(func=cmd_probe)

    return ap


def main(argv: Optional[List[str]] = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
