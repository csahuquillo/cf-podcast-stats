#!/usr/bin/env python3
"""
cf-podcast-stats — weekly request analytics for a self-hosted podcast.

Parses CloudFront access logs, requests aggregate counts from Cloudflare R2 Data Access Logs, or
counts MP3 GETs at Cloudflare's edge. The edge source includes cache hits; the R2 source counts
object requests that reached R2. Neither aggregate source identifies unique listeners or confirmed
plays.

Designed to run on a schedule (systemd timer / cron), e.g. Sunday night.

Everything is configured via environment variables (see config.example.env). The log source
can be legacy CloudFront standard logs or Cloudflare R2 Data Access Logs.

Requirements: awscli (for CloudFront S3 logs), and for email either the Gmail API
(google-api-python-client) or a plain SMTP server. See README.md.

MIT License.
"""
import base64, glob, gzip, json, os, re, smtplib, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

# ─────────────────────────── config (env vars) ───────────────────────────
def _load_dotenv():
    """Load KEY=VALUE lines from a .env file next to this script or at CFPS_ENV."""
    p = os.environ.get("CFPS_ENV") or str(Path(__file__).resolve().parent / ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()
def env(k, default=None): return os.environ.get(k, default)

BUCKET       = env("CFPS_LOG_BUCKET")                       # bucket con los logs de CloudFront
PREFIX       = env("CFPS_LOG_PREFIX", "")                   # prefijo dentro del bucket (p.ej. "cf-logs/")
SOURCE       = env("CFPS_SOURCE", "cloudfront").lower()
CF_API_TOKEN = env("CFPS_CLOUDFLARE_API_TOKEN", "")
CF_API_TOKEN_SSM_PARAMETER = env("CFPS_CLOUDFLARE_API_TOKEN_SSM_PARAMETER", "")
CFPS_AWS_REGION = env("CFPS_AWS_REGION", "eu-west-3")
CF_ACCOUNT   = env("CFPS_CLOUDFLARE_ACCOUNT_TAG", "")
CF_ZONE_TAG  = env("CFPS_CLOUDFLARE_ZONE_TAG", "")
PODCAST_HOST = env("CFPS_PODCAST_HOST", "")
R2_BUCKET    = env("CFPS_R2_BUCKET", "")
R2_DATASET   = env("CFPS_R2_DATASET", "r2")
WINDOW_DAYS  = int(env("CFPS_WINDOW_DAYS", "7"))
EPISODE_RE   = env("CFPS_EPISODE_REGEX", r"/([^/]+?)\.mp3")  # captura el "id" del episodio de la URI
BRAND        = env("CFPS_BRAND", "My Podcast")
FEED_URL     = env("CFPS_FEED_URL", "")                     # RSS opcional para poner títulos de episodio
CF_HOST      = env("CFPS_CF_HOST", "")                      # solo para mostrar la fuente en el email
GEOIP        = env("CFPS_GEOIP", "1") == "1"
# IPs a ignorar: p.ej. tu propio servidor web si hace de proxy del MP3 (se geolocalizaría
# como el país del servidor en vez del oyente real). Separadas por comas.
EXCLUDE_IPS  = {x.strip() for x in env("CFPS_EXCLUDE_IPS", "").split(",") if x.strip()}
STATE_DIR    = Path(env("CFPS_STATE_DIR", str(Path(__file__).resolve().parent / "state")))

EMAIL_TO     = env("CFPS_EMAIL_TO")
EMAIL_FROM   = env("CFPS_EMAIL_FROM", EMAIL_TO)
EMAIL_METHOD = env("CFPS_EMAIL_METHOD", "smtp")            # "smtp" | "gmail_api" | "none"
GOOGLE_TOKEN = env("CFPS_GOOGLE_TOKEN", "")                # para gmail_api
SMTP_HOST    = env("CFPS_SMTP_HOST"); SMTP_PORT = int(env("CFPS_SMTP_PORT", "587"))
SMTP_USER    = env("CFPS_SMTP_USER"); SMTP_PASS = env("CFPS_SMTP_PASS")
SMTP_TLS     = env("CFPS_SMTP_TLS", "1") == "1"

GEO_CACHE = STATE_DIR / "geoip_cache.json"
STATE     = STATE_DIR / "report_state.json"
LOGDIR    = STATE_DIR / "logs"


def log(m=""): print(m, flush=True)


# ─────────────────────────── S3 log download ───────────────────────────
def sync_logs(cutoff):
    """Un único `aws s3 sync` filtrado por las fechas de la ventana (antes: un `aws s3 cp`
    por fichero, ~17 min para ~1.100 logs)."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    cmd = ["aws", "s3", "sync", f"s3://{BUCKET}/{PREFIX}", str(LOGDIR), "--only-show-errors",
           "--exclude", "*"]
    d = (cutoff - timedelta(days=1)).date()
    while d <= datetime.now(timezone.utc).date():
        cmd += ["--include", f"*.{d.isoformat()}-*"]    # CloudFront: name.YYYY-MM-DD-HH.hash.gz
        d += timedelta(days=1)
    subprocess.run(cmd, check=False)
    return len(glob.glob(str(LOGDIR / "*.gz")))


# ─────────────────────────── Cloudflare R2 Data Access Logs ───────────────────────────
CF_API = "https://api.cloudflare.com/client/v4"
def _load_cloudflare_api_token():
    """Load an R2 query token from process config or a protected SSM SecureString."""
    if CF_API_TOKEN:
        return CF_API_TOKEN
    if not CF_API_TOKEN_SSM_PARAMETER:
        raise RuntimeError("R2 source requires a Cloudflare token or CFPS_CLOUDFLARE_API_TOKEN_SSM_PARAMETER")
    try:
        result = subprocess.run(
            ["aws", "ssm", "get-parameter", "--name", CF_API_TOKEN_SSM_PARAMETER,
             "--with-decryption", "--region", CFPS_AWS_REGION,
             "--query", "Parameter.Value", "--output", "text"],
            check=True, capture_output=True, text=True, timeout=20,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError("AWS SSM could not retrieve the Cloudflare Observability token") from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"AWS SSM token retrieval failed ({type(exc).__name__})") from None
    token = result.stdout.strip()
    if not token or token == "None":
        raise RuntimeError("AWS SSM returned an empty Cloudflare Observability token")
    return token


def _cf_request(url, payload=None, token=None):
    """Call Cloudflare without ever including credentials or response data in errors."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token or CF_API_TOKEN}",
                 "Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Cloudflare error bodies can contain account details; do not print them.
        raise RuntimeError(f"Cloudflare API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cloudflare Observability API request failed ({type(exc).__name__})") from None


def _r2_filter(key, operation, value, kind):
    return {"key": key, "operation": operation, "value": value, "type": kind, "kind": "filter"}


def fetch_r2_aggregates(cutoff):
    """Fetch aggregate counts only; never request individual R2 log events or PII fields."""
    if not CF_ACCOUNT or not R2_BUCKET:
        raise RuntimeError("R2 source requires CFPS_CLOUDFLARE_ACCOUNT_TAG and CFPS_R2_BUCKET")
    if len(CF_ACCOUNT) != 32 or not re.fullmatch(r"[0-9a-f]{32}", CF_ACCOUNT):
        raise RuntimeError("CFPS_CLOUDFLARE_ACCOUNT_TAG must be a 32-character lowercase account id")
    if not 1 <= WINDOW_DAYS <= 7:
        raise RuntimeError("R2 Data Access Logs retain seven days; CFPS_WINDOW_DAYS must be between 1 and 7")
    end = datetime.now(timezone.utc)
    filters = [
        _r2_filter("bucket", "eq", R2_BUCKET, "string"),
        _r2_filter("interface", "eq", "Public", "string"),
        _r2_filter("action", "eq", "GetObject", "string"),
        _r2_filter("request.method", "eq", "GET", "string"),
        _r2_filter("response.status", "in", "200,206", "number"),
        _r2_filter("request.uri", "ends_with", ".mp3", "string"),
    ]
    payload = {
        "queryId": "cf-podcast-stats-weekly-r2",
        "timeframe": {"from": int(cutoff.timestamp() * 1000), "to": int(end.timestamp() * 1000)},
        "dry": True,
        "ignoreSeries": True,
        "limit": 2000,
        "parameters": {
            "datasets": [R2_DATASET],
            "filters": filters,
            "filterCombination": "and",
            "calculations": [{"operator": "count", "alias": "observed_requests"}],
            "groupBys": [{"type": "string", "value": "request.uri"}],
        },
        "view": "calculations",
    }
    response = _cf_request(
        f"{CF_API}/accounts/{urllib.parse.quote(CF_ACCOUNT, safe='')}/workers/observability/telemetry/query",
        payload, token=_load_cloudflare_api_token(),
    )
    result = response.get("result") or {}
    if response.get("success") is not True or not isinstance(result.get("calculations"), list):
        raise RuntimeError("Cloudflare Observability API returned an unexpected aggregate result")

    per_episode = Counter()
    total = 0
    for calculation in result["calculations"]:
        for aggregate in calculation.get("aggregates") or []:
            groups = aggregate.get("groups") or []
            uri = next((str(group.get("value") or "") for group in groups
                        if group.get("key") == "request.uri"), "")
            match = re.search(EPISODE_RE, uri.split("?", 1)[0])
            if not match:
                continue
            count = aggregate.get("value")
            if isinstance(count, (int, float)) and count >= 0:
                per_episode[match.group(1)] += count
                total += count
    log(f"[r2] aggregate MP3 GET requests={total}, episodes={len(per_episode)}")
    return per_episode, total


def fetch_edge_aggregates(cutoff):
    """Count public MP3 GETs at Cloudflare's edge, including responses served from cache."""
    if not CF_ZONE_TAG or not re.fullmatch(r"[0-9a-f]{32}", CF_ZONE_TAG):
        raise RuntimeError("CFPS_CLOUDFLARE_ZONE_TAG must be the 32-character zone id")
    if not PODCAST_HOST:
        raise RuntimeError("CFPS_PODCAST_HOST is required for the Cloudflare edge source")
    if not 1 <= WINDOW_DAYS <= 31:
        raise RuntimeError("CFPS_WINDOW_DAYS must be between 1 and 31 for Cloudflare edge analytics")

    query = """query PodcastMP3Requests($zoneTag: String!, $filter: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!) {
      viewer {
        zones(filter: {zoneTag: $zoneTag}) {
          requests: httpRequestsAdaptiveGroups(limit: 10000, filter: $filter) {
            count
            avg { sampleInterval }
            dimensions { clientRequestPath }
          }
        }
      }
    }"""
    payload = {
        "query": query,
        "variables": {
            "zoneTag": CF_ZONE_TAG,
            "filter": {
                "datetime_geq": cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "datetime_lt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "requestSource": "eyeball",
                "clientRequestHTTPHost": PODCAST_HOST,
                "clientRequestHTTPMethodName": "GET",
                "edgeResponseStatus_in": [200, 206],
                "clientRequestPath_like": "%.mp3",
            },
        },
    }
    response = _cf_request("https://api.cloudflare.com/client/v4/graphql", payload,
                           token=_load_cloudflare_api_token())
    if response.get("errors"):
        raise RuntimeError("Cloudflare GraphQL analytics query failed")
    try:
        zones = response["data"]["viewer"]["zones"]
        if not zones:
            raise ValueError
        groups = zones[0]["requests"]
    except (KeyError, TypeError, IndexError, ValueError):
        raise RuntimeError("Cloudflare GraphQL returned an unexpected analytics result") from None

    per_episode = Counter()
    total = 0
    sample_intervals = []
    for group in groups:
        path = str((group.get("dimensions") or {}).get("clientRequestPath") or "")
        match = re.search(EPISODE_RE, path.split("?", 1)[0])
        if not match:
            continue
        count = int(group.get("count") or 0)
        if count <= 0:
            continue
        per_episode[match.group(1)] += count
        total += count
        interval = (group.get("avg") or {}).get("sampleInterval")
        if interval:
            sample_intervals.append(float(interval))
    return per_episode, total, max(sample_intervals, default=1.0)


BOT = "Bots / crawlers"


# ─────────────────────────── user-agent → app family ───────────────────────────
def ua_family(ua):
    l = urllib.parse.unquote(ua).lower()
    table = [
        ("airable", "Airable (directory → cars/speakers)"), ("spotify", "Spotify"),
        ("overcast", "Overcast"), ("pocketcasts", "Pocket Casts"), ("pocket casts", "Pocket Casts"),
        ("castbox", "Castbox"), ("antennapod", "AntennaPod"), ("podcast addict", "Podcast Addict"),
        ("podcastaddict", "Podcast Addict"), ("castro", "Castro"), ("podverse", "Podverse"),
        ("amazon", "Amazon / Alexa"), ("alexa", "Amazon / Alexa"), ("audible", "Amazon / Alexa"),
        ("gpodder", "Google Podcasts"), ("google-podcast", "Google Podcasts"), ("iheart", "iHeartRadio"),
        ("podcasts/", "Apple Podcasts"), ("itunes", "Apple Podcasts"), ("applecoremedia", "Apple Podcasts"),
        ("atc/", "Apple Podcasts"), ("itms", "Apple Podcasts"),
    ]
    for needle, name in table:
        if needle in l:
            return name
    if "bot" in l or "guzzle" in l or "headless" in l or "crawler" in l or "spider" in l or "python" in l or "curl" in l or "wget" in l or "go-http" in l:
        return BOT
    if l.startswith("mozilla") or "chrome" in l or "safari" in l or "firefox" in l:
        return "Web browser"
    return "Other"


# ─────────────────────────── geoip (ip-api batch, cached) ───────────────────────────
def geolocate(ips):
    cache = {}
    if GEO_CACHE.exists():
        try: cache = json.loads(GEO_CACHE.read_text())
        except Exception: pass
    if GEOIP:
        todo = [ip for ip in ips if ip not in cache]
        for i in range(0, len(todo), 100):
            batch = todo[i:i+100]
            try:
                req = urllib.request.Request("http://ip-api.com/batch?fields=query,countryCode,country",
                                             data=json.dumps(batch).encode(), headers={"Content-Type": "application/json"})
                for r in json.loads(urllib.request.urlopen(req, timeout=20).read()):
                    cache[r.get("query")] = {"cc": r.get("countryCode") or "??", "country": r.get("country") or "Unknown"}
            except Exception as e:
                log(f"[geo] batch failed: {e}")
                for ip in batch: cache.setdefault(ip, {"cc": "??", "country": "Unknown"})
            time.sleep(4)   # ip-api free tier: 15 req/min
        GEO_CACHE.write_text(json.dumps(cache))
    return cache


def flag(cc):
    if not cc or len(cc) != 2 or cc == "??": return "🏳️"
    return chr(0x1F1E6 + ord(cc[0].upper()) - 65) + chr(0x1F1E6 + ord(cc[1].upper()) - 65)


# ─────────────────────────── episode titles (optional RSS) ───────────────────────────
def episode_titles():
    titles = {}
    if not FEED_URL:
        return titles
    try:
        request = urllib.request.Request(
            FEED_URL,
            headers={"User-Agent": "cf-podcast-stats/1.0 (+https://github.com/csahuquillo/cf-podcast-stats)"},
        )
        data = urllib.request.urlopen(request, timeout=20).read().decode("utf-8", "ignore")
        for item in re.findall(r"<item>(.*?)</item>", data, re.DOTALL):
            t = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
            enc = re.search(r'url="([^"]+\.mp3)"', item) or re.search(r"<enclosure[^>]+/([^/\"]+\.mp3)", item)
            if t and enc:
                m = re.search(EPISODE_RE, enc.group(1))
                if m:
                    titles[m.group(1)] = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)).strip()
    except Exception as e:
        log(f"[titles] {e}")
    return titles


# ─────────────────────────── parse ───────────────────────────
def parse(cutoff):
    dls = []; total_req = 0; excluded = 0
    for fn in glob.glob(str(LOGDIR / "*.gz")):
        try:
            with gzip.open(fn, "rt", errors="replace") as f:
                for line in f:
                    if line.startswith("#"):
                        continue
                    p = line.rstrip("\n").split("\t")
                    if len(p) < 11:
                        continue
                    date, ip, method, uri, status, ua = p[0], p[4], p[5], p[7], p[8], p[10]
                    if datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
                        continue
                    total_req += 1
                    if method != "GET" or status not in ("200", "206") or not uri.endswith(".mp3"):
                        continue
                    m = re.search(EPISODE_RE, uri)
                    if not m:
                        continue
                    if ip in EXCLUDE_IPS:
                        excluded += 1
                        continue
                    dls.append((date, ip, ua_family(ua), m.group(1)))
        except Exception as e:
            log(f"[parse] {fn}: {e}")
    return dls, total_req, excluded


# ─────────────────────────── HTML dossier ───────────────────────────
def _bar(pct, color):
    pct = max(2, min(100, pct))
    return (f'<div style="background:#eceff4;border-radius:4px;height:9px;">'
            f'<div style="background:{color};height:9px;border-radius:4px;width:{pct:.0f}%;"></div></div>')

def build_html(dls, total_req, cutoff, titles, cum, excluded=0):
    bots = len(set((ip, app, ep, date) for (date, ip, app, ep) in dls if app == BOT))
    dls = [d for d in dls if d[2] != BOT]                               # bots fuera de todas las cifras
    uniq = set((ip, app, ep, date) for (date, ip, app, ep) in dls)      # dedupe IAB-ish
    downloads = len(uniq)
    LISTENER = {"Apple Podcasts","Spotify","Overcast","Pocket Casts","Castbox","AntennaPod",
                "Podcast Addict","Castro","Podverse","Amazon / Alexa","Google Podcasts","iHeartRadio",
                "Airable (directory → cars/speakers)","Web browser"}
    listeners = len(set(ip for (_, ip, app, ep) in dls if app in LISTENER))
    per_ep = Counter(); per_app = Counter(); per_day = Counter()
    for (ip, app, ep, date) in uniq:
        per_ep[ep] += 1; per_app[app] += 1; per_day[date] += 1
    geo_units = set((ip, ep, date) for (date, ip, app, ep) in dls)
    geo = geolocate(sorted(set(ip for (ip, ep, date) in geo_units)))
    per_country = Counter(geo.get(ip, {}).get("cc", "??") for (ip, ep, date) in geo_units)

    win = f'{cutoff.strftime("%d %b %Y")} → {datetime.now(timezone.utc).strftime("%d %b %Y")}'
    cum_total = cum.get("downloads_total", 0) + downloads
    weeks = cum.get("weeks", 0) + 1
    muted = "#8a93a6"

    def title_of(ep): return titles.get(ep) or ep.replace("-", " ").strip().capitalize()
    def rows(counter, color, namer, top=10):
        mx = max(counter.values()) if counter else 1
        return "".join(
            f'<tr><td style="padding:7px 6px;font-size:13px;color:#1a2540;">{namer(k)}</td>'
            f'<td style="padding:7px 6px;width:120px;">{_bar(100*v/mx, color)}</td>'
            f'<td style="padding:7px 6px;text-align:right;font-weight:700;color:#0f1729;">{v}</td></tr>'
            for k, v in counter.most_common(top))

    top_eps = rows(per_ep, "#39d353", lambda k: f"<b>{title_of(k)[:54]}</b>")
    cc_name = {g["cc"]: g["country"] for g in geo.values()}
    top_c = rows(per_country, "#4b8bff", lambda cc: f'{flag(cc)} {cc_name.get(cc, cc)}', 8)
    top_a = rows(per_app, "#a06bff", lambda a: a, 9)

    days = sorted(per_day); mxd = max(per_day.values()) if per_day else 1
    spark = "".join(
        f'<td style="vertical-align:bottom;text-align:center;padding:0 3px;">'
        f'<div style="background:#39d353;width:26px;border-radius:3px 3px 0 0;height:{max(4,int(70*per_day[d]/mxd))}px;margin:0 auto;"></div>'
        f'<div style="font-size:10px;color:{muted};margin-top:3px;">{d[8:10]}/{d[5:7]}</div>'
        f'<div style="font-size:11px;color:#0f1729;font-weight:700;">{per_day[d]}</div></td>' for d in days)
    kpi = lambda v, l: (f'<td style="text-align:center;padding:10px 8px;"><div style="font-size:30px;font-weight:800;color:#0f1729;">{v}</div>'
                        f'<div style="font-size:12px;color:{muted};text-transform:uppercase;letter-spacing:.5px;">{l}</div></td>')
    if SOURCE == "r2":
        src = f" · source: Cloudflare R2 Data Access Logs ({R2_BUCKET})"
    else:
        src = f" · source: CloudFront ({CF_HOST})" if CF_HOST else " · source: CloudFront logs"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f4f6fa;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a2540;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:22px 12px;">
<table width="660" cellpadding="0" cellspacing="0" style="max-width:660px;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
  <tr><td style="background:#0f1729;padding:26px 30px;">
    <div style="color:#39d353;font-size:13px;font-weight:700;letter-spacing:1px;">📡 {BRAND.upper()} · PODCAST</div>
    <div style="color:#fff;font-size:24px;font-weight:800;margin-top:4px;">Weekly download report</div>
    <div style="color:#8a93a6;font-size:13px;margin-top:4px;">{win}{src}</div></td></tr>
  <tr><td style="padding:8px 20px 0;"><table width="100%"><tr>
    {kpi(f"{downloads:,}", "Downloads")}{kpi(f"{listeners:,}", "Unique listeners")}{kpi(len(per_ep), "Episodes")}{kpi(len(per_country), "Countries")}</tr></table></td></tr>
  <tr><td style="padding:14px 30px 6px;"><div style="font-size:15px;font-weight:800;color:#0f1729;margin-bottom:8px;">📈 Downloads per day</div>
    <table width="100%" style="height:110px;"><tr>{spark}</tr></table></td></tr>
  <tr><td style="padding:14px 30px 6px;"><div style="font-size:15px;font-weight:800;color:#0f1729;margin-bottom:6px;">🎙️ Top episodes</div>
    <table width="100%" cellpadding="0" cellspacing="0">{top_eps}</table></td></tr>
  <tr><td style="padding:14px 30px 6px;"><table width="100%"><tr>
    <td width="50%" valign="top" style="padding-right:10px;"><div style="font-size:15px;font-weight:800;color:#0f1729;margin-bottom:6px;">🌍 By country</div>
      <table width="100%" cellpadding="0" cellspacing="0">{top_c}</table></td>
    <td width="50%" valign="top" style="padding-left:10px;"><div style="font-size:15px;font-weight:800;color:#0f1729;margin-bottom:6px;">📱 By app / platform</div>
      <table width="100%" cellpadding="0" cellspacing="0">{top_a}</table></td></tr></table></td></tr>
  <tr><td style="padding:16px 30px;"><div style="background:#f0f7ff;border-left:3px solid #4b8bff;border-radius:8px;padding:12px 16px;font-size:13px;color:#3a4a63;">
    <b>All-time (since you started measuring):</b> {cum_total:,} downloads across {weeks} week(s) · {total_req:,} raw requests this week.
    <br>Excluded: {bots:,} bot/crawler downloads{f" · {excluded:,} requests from excluded IPs (own server proxy)" if excluded else ""}.</div></td></tr>
  <tr><td style="padding:6px 30px 26px;font-size:11px;color:#8a93a6;line-height:1.6;">
    <b>Methodology:</b> an observed download request = a GET request to an episode .mp3 (HTTP 200/206) deduplicated by IP + app + episode + day
    (IAB-style). Geolocation is approximate (ip-api). Bots/crawlers and CFPS_EXCLUDE_IPS are excluded. Includes app and directory downloads (e.g. Airable resells to cars/speakers).
    R2 Data Access Logs are asynchronous and best effort; events can be delayed or omitted, so counts are observed requests, not confirmed listens or a complete billing record.
    Release-day spikes include directory prefetch, not all are human listens. — generated by
    <a href="https://github.com/csahuquillo/cf-podcast-stats" style="color:#8a93a6;">cf-podcast-stats</a>.
  </td></tr>
</table></td></tr></table></body></html>"""


def build_r2_html(per_episode, total_requests, cutoff, titles):
    """Render an honest report from R2 aggregate data without listener-level claims."""
    end = datetime.now(timezone.utc)
    win = f'{cutoff.strftime("%d %b %Y")} → {end.strftime("%d %b %Y")}'
    muted = "#687386"

    def title_of(episode):
        return titles.get(episode) or episode.replace("-", " ").strip().capitalize()

    maximum = max(per_episode.values()) if per_episode else 1
    rows = "".join(
        f'<tr><td style="padding:8px 6px;font-size:13px;color:#1a2540;">{title_of(episode)}</td>'
        f'<td style="padding:7px 6px;width:150px;">{_bar(100 * count / maximum, "#39d353")}</td>'
        f'<td style="padding:7px 6px;text-align:right;font-weight:700;color:#0f1729;">{count:,.0f}</td></tr>'
        for episode, count in per_episode.most_common(20)
    ) or '<tr><td colspan="3" style="padding:12px;color:#687386;">No se observaron peticiones MP3 en este periodo.</td></tr>'

    def kpi(value, label):
        return (f'<td style="text-align:center;padding:14px 8px;">'
                f'<div style="font-size:30px;font-weight:800;color:#0f1729;">{value}</div>'
                f'<div style="font-size:12px;color:{muted};text-transform:uppercase;letter-spacing:.5px;">{label}</div></td>')

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f4f6fa;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a2540;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:22px 12px;">
<table width="660" cellpadding="0" cellspacing="0" style="max-width:660px;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
  <tr><td style="background:#0f1729;padding:26px 30px;">
    <div style="color:#39d353;font-size:13px;font-weight:700;letter-spacing:1px;">📡 {BRAND.upper()} · PODCAST</div>
    <div style="color:#fff;font-size:24px;font-weight:800;margin-top:4px;">Resumen semanal de R2</div>
    <div style="color:#8a93a6;font-size:13px;margin-top:4px;">{win} · Cloudflare R2 ({R2_BUCKET})</div></td></tr>
  <tr><td style="padding:14px 20px 0;"><table width="100%"><tr>
    {kpi(f"{total_requests:,.0f}", "Peticiones GET MP3 observadas")}
    {kpi(len(per_episode), "Episodios solicitados")}</tr></table></td></tr>
  <tr><td style="padding:14px 30px 6px;"><div style="font-size:15px;font-weight:800;color:#0f1729;margin-bottom:6px;">🎙️ Peticiones por episodio</div>
    <table width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>
  <tr><td style="padding:16px 30px;"><div style="background:#fff8e8;border-left:3px solid #e9a23b;border-radius:8px;padding:12px 16px;font-size:13px;color:#5c4b2f;">
    Este resumen cuenta peticiones públicas GET a archivos MP3 con respuesta 200 o 206. Una descarga con rangos puede generar varias peticiones; no equivale a escuchas ni a oyentes únicos.</div></td></tr>
  <tr><td style="padding:6px 30px 26px;font-size:11px;color:#8a93a6;line-height:1.6;">
    Cloudflare entrega los Data Access Logs de forma asíncrona y con mejor esfuerzo, y los conserva siete días. Por ello puede haber retrasos u omisiones. El informe usa agregados y no procesa IP ni agente de usuario; no ofrece desglose por país, aplicación o bots.
    — generado por <a href="https://github.com/csahuquillo/cf-podcast-stats" style="color:#8a93a6;">cf-podcast-stats</a>.
  </td></tr>
</table></td></tr></table></body></html>"""


def build_edge_html(per_episode, total_requests, sample_interval, cutoff, titles):
    """Render request counts measured at Cloudflare's edge, including cache hits."""
    end = datetime.now(timezone.utc)
    win = f'{cutoff.strftime("%d %b %Y")} → {end.strftime("%d %b %Y")}'
    muted = "#687386"

    def title_of(episode):
        return escape(titles.get(episode) or episode.replace("-", " ").strip().capitalize())

    maximum = max(per_episode.values()) if per_episode else 1
    rows = "".join(
        f'<tr><td style="padding:8px 6px;font-size:13px;color:#1a2540;">{title_of(episode)}</td>'
        f'<td style="padding:7px 6px;width:150px;">{_bar(100 * count / maximum, "#39d353")}</td>'
        f'<td style="padding:7px 6px;text-align:right;font-weight:700;color:#0f1729;">{count:,.0f}</td></tr>'
        for episode, count in per_episode.most_common(20)
    ) or '<tr><td colspan="3" style="padding:12px;color:#687386;">No se observaron peticiones MP3 en este periodo.</td></tr>'

    def kpi(value, label):
        return (f'<td style="text-align:center;padding:14px 8px;">'
                f'<div style="font-size:30px;font-weight:800;color:#0f1729;">{value}</div>'
                f'<div style="font-size:12px;color:{muted};text-transform:uppercase;letter-spacing:.5px;">{label}</div></td>')

    sample_note = (f'Cloudflare indica un intervalo de muestreo máximo de 1:{sample_interval:g}; '
                   'el total puede ser estimado.' if sample_interval > 1 else
                   'Cloudflare no indicó muestreo para los grupos devueltos.')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f4f6fa;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a2540;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:22px 12px;">
<table width="660" cellpadding="0" cellspacing="0" style="max-width:660px;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
  <tr><td style="background:#0f1729;padding:26px 30px;">
    <div style="color:#39d353;font-size:13px;font-weight:700;letter-spacing:1px;">📡 {escape(BRAND.upper())} · PODCAST</div>
    <div style="color:#fff;font-size:24px;font-weight:800;margin-top:4px;">Resumen semanal de peticiones</div>
    <div style="color:#8a93a6;font-size:13px;margin-top:4px;">{win} · Cloudflare edge ({escape(PODCAST_HOST)})</div></td></tr>
  <tr><td style="padding:14px 20px 0;"><table width="100%"><tr>
    {kpi(f"{total_requests:,.0f}", "Peticiones GET MP3 en el edge")}
    {kpi(len(per_episode), "Episodios solicitados")}</tr></table></td></tr>
  <tr><td style="padding:14px 30px 6px;"><div style="font-size:15px;font-weight:800;color:#0f1729;margin-bottom:6px;">🎙️ Peticiones por episodio</div>
    <table width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>
  <tr><td style="padding:16px 30px;"><div style="background:#fff8e8;border-left:3px solid #e9a23b;border-radius:8px;padding:12px 16px;font-size:13px;color:#5c4b2f;">
    El contador incluye respuestas desde la caché de Cloudflare y puede contar varias peticiones por escucha (por ejemplo, rangos 206), descargas repetidas y bots. No representa oyentes únicos ni escuchas confirmadas. {sample_note}</div></td></tr>
  <tr><td style="padding:6px 30px 26px;font-size:11px;color:#8a93a6;line-height:1.6;">
    Solo se cuentan GET públicos a archivos MP3 del host indicado que respondieron 200 o 206. No se procesan IP ni agentes de usuario. — generado por
    <a href="https://github.com/csahuquillo/cf-podcast-stats" style="color:#8a93a6;">cf-podcast-stats</a>.
  </td></tr>
</table></td></tr></table></body></html>"""


# ─────────────────────────── email ───────────────────────────
def send_email(html, subject):
    msg = MIMEText(html, "html", "utf-8")
    msg["to"] = EMAIL_TO; msg["from"] = EMAIL_FROM; msg["subject"] = subject
    if EMAIL_METHOD == "none":
        (STATE_DIR / "last_report.html").write_text(html); log("[email] method=none → saved last_report.html"); return
    if EMAIL_METHOD == "gmail_api":
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        d = json.loads(Path(GOOGLE_TOKEN).read_text())
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN, d["scopes"])
        if not creds.valid: creds.refresh(Request())
        build("gmail", "v1", credentials=creds).users().messages().send(
            userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
    else:  # smtp
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        if SMTP_TLS: s.starttls()
        if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string()); s.quit()


def main():
    test = "--test" in sys.argv
    dry_run = "--dry-run" in sys.argv
    if SOURCE not in ("cloudfront", "r2", "edge"):
        sys.exit("CFPS_SOURCE debe ser 'cloudfront', 'r2' o 'edge'.")
    if SOURCE == "cloudfront" and not BUCKET:
        sys.exit("CFPS_LOG_BUCKET no configurado (ver config.example.env / README).")
    if SOURCE == "cloudfront":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    if SOURCE == "r2":
        per_episode, total_requests = fetch_r2_aggregates(cutoff)
        html = build_r2_html(per_episode, total_requests, cutoff, episode_titles())
    elif SOURCE == "edge":
        per_episode, total_requests, sample_interval = fetch_edge_aggregates(cutoff)
        html = build_edge_html(per_episode, total_requests, sample_interval, cutoff, episode_titles())
    else:
        log(f"[logs] {sync_logs(cutoff)} files in range")
        dls, total_req, excluded = parse(cutoff)
        log(f"[parse] {len(dls)} download-lines, {total_req} raw requests, {excluded} from excluded IPs")
        cum = {}
        if STATE.exists():
            try: cum = json.loads(STATE.read_text())
            except Exception: pass
        html = build_html(dls, total_req, cutoff, episode_titles(), cum, excluded)
        if not test:
            uniq = len(set((ip, app, ep, date) for (date, ip, app, ep) in dls if app != BOT))
            cum["downloads_total"] = cum.get("downloads_total", 0) + uniq
            cum["weeks"] = cum.get("weeks", 0) + 1
            cum["last_run"] = datetime.now(timezone.utc).isoformat()
            STATE.write_text(json.dumps(cum))
        # prune old CloudFront logs
        for f in glob.glob(str(LOGDIR / "*.gz")):
            if (time.time() - os.path.getmtime(f)) > WINDOW_DAYS * 86400 + 172800:
                os.remove(f)
    subj = f"📊 {BRAND} — {'[TEST] ' if test else ''}weekly download report · {datetime.now().strftime('%d %b %Y')}"
    if dry_run:
        if SOURCE not in ("r2", "edge"):
            sys.exit("--dry-run solo está disponible para los informes R2 y edge.")
        log(f"[dry-run] informe {SOURCE} renderizado; no se envió correo ni se escribieron archivos")
        return
    send_email(html, subj)
    log(f"[email] sent to {EMAIL_TO} ({EMAIL_METHOD}): {subj}")


if __name__ == "__main__":
    main()
