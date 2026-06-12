#!/usr/bin/env python3
"""
ICP Appointment Monitor
Uses curl-cffi to impersonate Chrome TLS fingerprint, bypassing FortiGate IPS.
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from curl_cffi.requests import Session
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WATCHLIST_JSON  = os.environ.get("WATCHLIST_JSON", "[]")
STATE_FILE      = Path(os.environ.get("STATE_FILE", "data/state.json"))

BASE_URL = "https://icp.administracionelectronica.gob.es"
START_URL = f"{BASE_URL}/icpplus/index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def state_key(t: dict) -> str:
    return f"{t['province']}_{t.get('office','')}_{t['tramite']}"

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    import urllib.request
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log.error(f"Telegram error: {result}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

# ── HTML helpers ──────────────────────────────────────────────────────────────
def get_hidden_fields(soup) -> dict:
    """Extract all hidden input fields from a form."""
    return {
        inp["name"]: inp.get("value", "")
        for inp in soup.find_all("input", type="hidden")
        if inp.get("name")
    }

def find_form_action(soup, base_url: str) -> str:
    form = soup.find("form")
    if form and form.get("action"):
        return urljoin(base_url, form["action"])
    return base_url

def extract_result_text(soup) -> str:
    """Extract lot number / availability status from result page."""
    # Try known result containers
    for sel in ["#citaNoDisponible", ".mf-msg__info", ".mf-msg__exito",
                ".mf-msg__error", "#mensajeInfo", "#msgError",
                "#inicio", "div.mf-msg"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t and len(t) > 5:
                return t

    # Fallback: scan for keywords
    body = soup.get_text(" ", strip=True)
    keywords = ["lote", "expediente", "cita", "disponible", "número", "no hay",
                "appointment", "turno"]
    sentences = re.split(r'[.\n]', body)
    relevant = [s.strip() for s in sentences
                if any(k in s.lower() for k in keywords) and s.strip()]
    if relevant:
        return " | ".join(relevant[:3])

    # Last resort: first meaningful chunk
    chunks = [c.strip() for c in body.split() if len(c.strip()) > 3]
    return " ".join(chunks[:20]) if chunks else "No text found"

# ── Scraper ───────────────────────────────────────────────────────────────────
def scrape_lot(target: dict) -> str | None:
    province_code = str(target["province"])
    province_name = target["province_name"]
    office_name   = target["office_name"]
    tramite_kw    = target["tramite"].upper()

    with Session(impersonate="chrome124") as s:
        s.headers.update(HEADERS)

        # ── Step 1: Load main page ────────────────────────────────────────────
        log.info(f"  GET {START_URL}")
        r = s.get(START_URL, timeout=20)
        if r.status_code != 200:
            log.error(f"  Main page returned {r.status_code}")
            log.error(f"  Body: {r.text[:300]}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        hidden = get_hidden_fields(soup)

        # Find province option value (URL like /icpplus/citar?p=33&locale=es)
        sel = soup.find("select", id="form")
        if not sel:
            log.error("  select#form not found on main page")
            log.error(f"  Page body: {soup.get_text()[:300]}")
            return None

        province_value = None
        for opt in sel.find_all("option"):
            val  = opt.get("value", "")
            text = opt.get_text(strip=True)
            if f"p={province_code}&" in val or f"p={province_code}" == val.split("?")[-1].split("&")[0][2:]:
                province_value = val; break
            if province_name.lower() in text.lower():
                province_value = val; break

        if not province_value:
            opts = [(o.get("value",""), o.get_text(strip=True)) for o in sel.find_all("option")]
            log.error(f"  Province '{province_name}' not found. Options: {opts}")
            return None

        log.info(f"  Province value: {province_value}")

        # ── Step 2: Navigate to province URL (it's a full path, not POST) ────
        province_url = urljoin(BASE_URL, province_value)
        log.info(f"  GET {province_url}")
        r = s.get(province_url, timeout=20)
        if r.status_code != 200:
            log.error(f"  Province page {r.status_code}: {r.text[:200]}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        hidden = get_hidden_fields(soup)
        action = find_form_action(soup, province_url)

        # ── Step 3: Select office ─────────────────────────────────────────────
        sede_sel = soup.find("select", id="sede")
        if not sede_sel:
            log.error("  select#sede not found")
            log.error(f"  Page: {soup.get_text()[:400]}")
            return None

        office_value = None
        for opt in sede_sel.find_all("option"):
            val  = opt.get("value", "")
            text = opt.get_text(strip=True)
            if office_name.lower() in text.lower():
                office_value = val; break

        if not office_value:
            opts = [(o.get("value",""), o.get_text(strip=True)) for o in sede_sel.find_all("option")]
            log.error(f"  Office '{office_name}' not found. Options: {opts}")
            return None

        log.info(f"  Office value: {office_value}")

        # POST office selection
        post_data = {**hidden, "sede": office_value}
        log.info(f"  POST {action} sede={office_value}")
        r = s.post(action, data=post_data, timeout=20)
        if r.status_code != 200:
            log.error(f"  Office POST {r.status_code}: {r.text[:200]}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        hidden = get_hidden_fields(soup)
        action = find_form_action(soup, r.url)

        # ── Step 4: Select tramite ────────────────────────────────────────────
        tramite_value = None
        tramite_label = None

        # Try radio buttons
        for radio in soup.find_all("input", type="radio"):
            rid = radio.get("id", "")
            label = soup.find("label", attrs={"for": rid})
            label_text = label.get_text(strip=True).upper() if label else ""
            val = (radio.get("value") or "").upper()
            if tramite_kw in label_text or tramite_kw in val:
                tramite_value = radio.get("value")
                tramite_label = label_text or val
                tramite_name  = radio.get("name", "tramite")
                break

        if not tramite_value:
            log.error(f"  Tramite '{tramite_kw}' not found")
            radios_debug = []
            for r2 in soup.find_all("input", type="radio"):
                lbl = soup.find("label", attrs={"for": r2.get("id","")})
                radios_debug.append(lbl.get_text(strip=True) if lbl else r2.get("value",""))
            log.error(f"  Available tramites: {radios_debug}")
            return None

        log.info(f"  Tramite: {tramite_label}")

        post_data = {**hidden, tramite_name: tramite_value}
        log.info(f"  POST {action} tramite={tramite_value}")
        r = s.post(action, data=post_data, timeout=20)
        if r.status_code != 200:
            log.error(f"  Tramite POST {r.status_code}: {r.text[:200]}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # ── Step 5: Extract result ────────────────────────────────────────────
        result = extract_result_text(soup)
        log.info(f"  Result: {result}")
        return result

# ── Notification ──────────────────────────────────────────────────────────────
def build_notification(target: dict, old_text: str | None, new_text: str) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    expected = target.get("expected_lot", "").strip()

    highlight = ""
    if expected:
        nums = re.findall(r'\d+', new_text)
        for n in nums:
            if int(n) >= int(expected) if expected.isdigit() else False:
                highlight = f"⚡️ <b>Лот {n} ≥ ожидаемого ({expected})!</b>\n"
                break
        if expected.lower() in new_text.lower():
            highlight = "🎯 <b>ВАШ ЛОТ НАЙДЕН!</b>\n"

    change_line = f"📌 <i>Было:</i> {old_text}\n" if old_text else ""

    return (
        f"{highlight}"
        f"🔔 <b>Изменение на ICP!</b>\n"
        f"📍 {target['province_name']} → {target['office_name']}\n"
        f"📋 {target['tramite']}\n\n"
        f"{change_line}"
        f"✅ <b>Стало:</b> {new_text}\n\n"
        f"🕐 {now}\n"
        f"🔗 <a href='{START_URL}'>Открыть сайт</a>"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    watchlist = json.loads(WATCHLIST_JSON)
    if not watchlist:
        log.warning("WATCHLIST_JSON is empty")
        return

    state = load_state()
    log.info(f"Loaded state with {len(state)} entries. Checking {len(watchlist)} targets...")

    for target in watchlist:
        key = state_key(target)
        log.info(f"Checking: {target['province_name']} / {target['office_name']} / {target['tramite']}")

        try:
            new_text = scrape_lot(target)
        except Exception as e:
            log.error(f"  Unexpected error: {e}")
            new_text = None

        if new_text is None:
            log.warning(f"  Skipping {key}")
            continue

        old_text = state.get(key)
        if new_text != old_text:
            log.info(f"  CHANGE: {old_text!r} → {new_text!r}")
            send_telegram(build_notification(target, old_text, new_text))
            state[key] = new_text
            save_state(state)
        else:
            log.info(f"  No change")

    log.info("Done.")

if __name__ == "__main__":
    main()
