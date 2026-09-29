#!/usr/bin/env python3
"""
cf-podcast-stats — weekly download analytics for a self-hosted podcast on S3 + CloudFront.

Parses CloudFront access logs (stored in an S3 bucket), computes podcast-platform-style
metrics — IAB-ish deduplicated downloads, per episode, per country, per app/client, per
day, unique listeners — renders a nice HTML dossier and emails it to you.

Designed to run on a schedule (systemd timer / cron), e.g. Sunday night.

Everything is configured via environment variables (see config.example.env). No podcast
data is hardcoded, so anyone hosting a podcast on S3 + CloudFront can reuse it.

Requirements: awscli (for S3), and for email either the Gmail API
(google-api-python-client) or a plain SMTP server. See README.md.

MIT License.
"""
import base64, glob, gzip, json, os, re, smtplib, subprocess, sys, time
import urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
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
        data = urllib.request.urlopen(FEED_URL, timeout=20).read().decode("utf-8", "ignore")
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
    <b>Methodology:</b> a download = a GET request to an episode .mp3 (HTTP 200/206) deduplicated by IP + app + episode + day
    (IAB-style). Geolocation is approximate (ip-api). Bots/crawlers and CFPS_EXCLUDE_IPS are excluded. Includes app and directory downloads (e.g. Airable resells to cars/speakers).
    Release-day spikes include directory prefetch, not all are human listens. — generated by
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
    if not BUCKET:
        sys.exit("CFPS_LOG_BUCKET no configurado (ver config.example.env / README).")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
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
    # prune old logs
    for f in glob.glob(str(LOGDIR / "*.gz")):
        if (time.time() - os.path.getmtime(f)) > WINDOW_DAYS * 86400 + 172800:
            os.remove(f)
    subj = f"📊 {BRAND} — {'[TEST] ' if test else ''}weekly download report · {datetime.now().strftime('%d %b %Y')}"
    send_email(html, subj)
    log(f"[email] sent to {EMAIL_TO} ({EMAIL_METHOD}): {subj}")


if __name__ == "__main__":
    main()
