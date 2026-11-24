#!/usr/bin/env python3
"""
Bot d'alertes marché pour Joel : news, mouvements de prix, calendrier macro.
Envoie les alertes sur Telegram. Aucune installation : Python 3.9+ seulement.

Utilisation :
  1) python market_bot.py chatid   -> affiche ton CHAT_ID (après avoir écrit au bot)
  2) python market_bot.py          -> lance le bot (laisser tourner)
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

# ======================= RÉGLAGES (à remplir) =======================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "COLLE_TON_TOKEN_ICI"
CHAT_ID = os.environ.get("CHAT_ID") or "COLLE_TON_CHAT_ID_ICI"

CHECK_EVERY_SECONDS = 60          # fréquence de vérification
PRICE_ALERT_PERCENT = 0.5         # alerte si mouvement >= 0,5 % ...
PRICE_WINDOW_MINUTES = 15         # ... en 15 minutes
PRICE_COOLDOWN_MINUTES = 30       # pas deux alertes du même indice en 30 min
CALENDAR_WARN_MINUTES = 15        # prévenir 15 min avant un chiffre macro
NEWS_MAX_AGE_MINUTES = 120        # ignorer les news plus vieilles que ça
# =====================================================================

ZURICH = ZoneInfo("Europe/Zurich")
NEW_YORK = ZoneInfo("America/New_York")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; JoelMarketBot/1.0)"}

# Indices surveillés (futures Yahoo Finance)
SYMBOLS = {"NQ=F": "Nasdaq 100 (NAS100)", "ES=F": "S&P 500", "YM=F": "Dow Jones (DJ30)"}

# Flux de nouvelles
FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",          # CNBC Top News
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",           # CNBC Markets
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",     # MarketWatch
    "https://news.google.com/rss/search?q=Nvidia+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Fed+OR+Warsh+rate+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Trump+tariffs+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Iran+oil+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Nasdaq+OR+%22Wall+Street%22+when:1d&hl=en-US&gl=US&ceid=US:en",
]

# Mots-clés qui déclenchent une alerte (dans le titre)
KEYWORDS = [
    "nvidia", "jensen huang", "fed ", "federal reserve", "warsh", "rate hike", "rate cut",
    "fomc", "cpi", "inflation", "jobs report", "payrolls", "pce", "tariff", "trump",
    "iran", "oil", "selloff", "sell-off", "plunge", "plunges", "surge", "surges", "soar",
    "soars", "tumble", "tumbles", "crash", "rally", "rallies", "circuit breaker", "halt",
    "bitcoin", "china", "export ban", "chip", "openai", "microsoft", "apple", "amazon",
    "meta ", "google", "alphabet", "tesla", "earnings", "downgrade", "upgrade", "shutdown",
    "default", "bank failure", "emergency",
]

# Calendrier macro (heure de New York). Dates vérifiées au 9 nov. 2026 ;
# les dates PCE viennent d'un site tiers : à re-vérifier sur bea.gov.
EVENTS = [
    ("PCE (inflation préférée de la Fed)", datetime(2026, 11, 25, 8, 30, tzinfo=NEW_YORK)),
    ("NFP (rapport sur l'emploi)", datetime(2026, 12, 4, 8, 30, tzinfo=NEW_YORK)),
    ("FOMC (décision de la Fed)", datetime(2026, 12, 9, 14, 0, tzinfo=NEW_YORK)),
    ("CPI (inflation)", datetime(2026, 12, 10, 8, 30, tzinfo=NEW_YORK)),
    ("PCE (inflation préférée de la Fed)", datetime(2026, 12, 23, 8, 30, tzinfo=NEW_YORK)),
]


def log(msg):
    print(f"[{datetime.now(ZURICH).strftime('%d.%m %H:%M:%S')}] {msg}", flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true",
        "parse_mode": "HTML",
    }).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()
        return True
    except Exception as e:
        log(f"Erreur Telegram : {e}")
        return False


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "price_alert": {}, "cal_sent": []}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"Impossible d'écrire l'état : {e}")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------- NEWS ----------------------------
def fetch_feed(url):
    items = []
    root = ET.fromstring(http_get(url))
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(pub) if pub else None
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
        if title and link:
            items.append((title, link, dt))
    return items


def check_news(state, first_run):
    now = datetime.now(timezone.utc)
    seen = set(state["seen"])
    alerts = []
    for url in FEEDS:
        try:
            items = fetch_feed(url)
        except Exception as e:
            log(f"Flux en erreur ({url[:50]}...) : {e}")
            continue
        for title, link, dt in items:
            key = re.sub(r"\W+", "", title.lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            state["seen"].append(key)
            if first_run:
                continue
            if dt and now - dt > timedelta(minutes=NEWS_MAX_AGE_MINUTES):
                continue
            low = title.lower() + " "
            if any(k in low for k in KEYWORDS):
                when = dt.astimezone(ZURICH).strftime("%H:%M") if dt else "?"
                alerts.append((dt or now, title, link, when))
    alerts.sort(key=lambda a: a[0])
    for _, title, link, when in alerts[:6]:
        send_telegram(f"📰 <b>NEWS</b> ({when} heure suisse)\n{esc(title)}\n{link}")
        log(f"News envoyée : {title[:70]}")
    if len(alerts) > 6:
        send_telegram(f"📰 Et {len(alerts) - 6} autres news importantes (liste coupée).")


# --------------------------- PRIX -----------------------------
def get_series(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval=1m&range=1d")
    data = json.loads(http_get(url))
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    return [(t, c) for t, c in zip(ts, closes) if c is not None]


def check_prices(state):
    now_ts = time.time()
    for sym, name in SYMBOLS.items():
        try:
            series = get_series(sym)
        except Exception as e:
            log(f"Prix indisponible ({sym}) : {e}")
            continue
        if len(series) < 5:
            continue
        last_t, last_p = series[-1]
        if now_ts - last_t > 600:      # marché fermé / données vieilles
            continue
        target = last_t - PRICE_WINDOW_MINUTES * 60
        past = [p for t, p in series if t <= target]
        if not past:
            continue
        ref = past[-1]
        chg = (last_p - ref) / ref * 100
        last_alert = state["price_alert"].get(sym, 0)
        if abs(chg) >= PRICE_ALERT_PERCENT and now_ts - last_alert > PRICE_COOLDOWN_MINUTES * 60:
            arrow = "🟢 HAUSSE" if chg > 0 else "🔴 BAISSE"
            send_telegram(
                f"{arrow} <b>{name}</b>\n{chg:+.2f} % en {PRICE_WINDOW_MINUTES} min "
                f"(de {ref:,.0f} à {last_p:,.0f})\nRegarde les news : quelque chose vient de bouger."
            )
            state["price_alert"][sym] = now_ts
            log(f"Alerte prix {sym} {chg:+.2f}%")


# ------------------------- CALENDRIER -------------------------
def check_calendar(state):
    now = datetime.now(timezone.utc)
    for name, dt in EVENTS:
        key = f"{name}|{dt.isoformat()}"
        delta = (dt.astimezone(timezone.utc) - now).total_seconds() / 60
        if 0 < delta <= CALENDAR_WARN_MINUTES and key not in state["cal_sent"]:
            local = dt.astimezone(ZURICH).strftime("%H:%M")
            send_telegram(
                f"⚠️ <b>{name}</b> dans {int(delta)} min ({local} heure suisse).\n"
                "Gros mouvement possible : prudence, pas de trade juste avant."
            )
            state["cal_sent"].append(key)
            log(f"Alerte calendrier : {name}")


# ---------------------------- MAIN ----------------------------
def chatid_mode():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    data = json.loads(http_get(url))
    chats = {}
    for u in data.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat")
        if c:
            chats[c["id"]] = c.get("first_name") or c.get("title") or ""
    if not chats:
        print("Rien reçu. Ouvre ton bot dans Telegram, écris-lui 'salut', puis relance.")
    for cid, nm in chats.items():
        print(f"CHAT_ID = {cid}   ({nm})")


def main():
    if "COLLE_TON" in TELEGRAM_TOKEN or "COLLE_TON" in CHAT_ID:
        print("Remplis TELEGRAM_TOKEN et CHAT_ID en haut du fichier d'abord.")
        sys.exit(1)
    state = load_state()
    first_run = not state["seen"]
    send_telegram("✅ Bot marché démarré. Je surveille : news, prix (NAS/SP/DJ) et calendrier macro.")
    log("Bot démarré.")
    while True:
        try:
            check_news(state, first_run)
            first_run = False
            check_prices(state)
            check_calendar(state)
            save_state(state)
        except Exception as e:
            log(f"Erreur : {e}")
        time.sleep(CHECK_EVERY_SECONDS)


def run_once():
    """Un seul passage (pour GitHub Actions, lancé toutes les 5 minutes)."""
    global CHAT_ID
    CHAT_ID = CHAT_ID or "COLLE_TON_CHAT_ID_ICI"
    if "COLLE_TON" in TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN manquant.")
        sys.exit(1)
    state = load_state()
    if "COLLE_TON" in CHAT_ID:
        # CHAT_ID non fourni : on le retrouve automatiquement (il faut avoir écrit au bot)
        cid = state.get("chat_id")
        if not cid:
            try:
                data = json.loads(http_get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"))
                for u in data.get("result", []):
                    c = (u.get("message") or {}).get("chat")
                    if c:
                        cid = c["id"]
                        break
            except Exception as e:
                log(f"getUpdates en erreur : {e}")
        if not cid:
            print("Chat introuvable : ouvre ton bot dans Telegram et écris-lui 'salut'.")
            sys.exit(1)
        state["chat_id"] = cid
        CHAT_ID = str(cid)
    first_run = not state["seen"]
    if first_run:
        send_telegram("✅ Bot marché démarré. Je surveille : news, prix (NAS/SP/DJ) et calendrier macro.")
    check_news(state, first_run)
    check_prices(state)
    check_calendar(state)
    save_state(state)
    log("Passage terminé.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "chatid":
        chatid_mode()
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        main()
