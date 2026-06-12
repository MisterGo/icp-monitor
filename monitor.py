#!/usr/bin/env python3
"""
ICP Appointment Monitor — uses system Chrome via Playwright.
System Chrome bypasses FortiGate/F5 bot detection that blocks Chromium.
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WATCHLIST_JSON   = os.environ.get("WATCHLIST_JSON", "[]")
STATE_FILE       = Path(os.environ.get("STATE_FILE", "data/state.json"))

START_URL = "https://icp.administracionelectronica.gob.es/icpplus/index.html"

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
    return f"{t['province']}_{t.get('office', '')}_{t['tramite']}"

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

# ── Scraper ───────────────────────────────────────────────────────────────────
async def scrape_lot(page, target: dict) -> str | None:
    province_code = str(target["province"])
    province_name = target["province_name"]
    office_name   = target["office_name"]
    tramite_kw    = target["tramite"].upper()

    try:
        # ── Step 1: Load main page ────────────────────────────────────────────
        log.info(f"  Loading {START_URL}")
        await page.goto(START_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_selector("select#form", timeout=30000)

        # ── Step 2: Select province ───────────────────────────────────────────
        options = await page.query_selector_all("select#form option")
        province_value = None
        for opt in options:
            val  = await opt.get_attribute("value") or ""
            text = (await opt.inner_text()).strip()
            if f"p={province_code}&" in val or f"p={province_code}" in val:
                province_value = val; break
            if province_name.lower() in text.lower():
                province_value = val; break

        if not province_value:
            opts_dbg = [(await o.get_attribute("value"), (await o.inner_text()).strip()) for o in options]
            log.error(f"  Province not found. Options: {opts_dbg}")
            return None

        log.info(f"  Province: {province_value}")
        await page.select_option("select#form", value=province_value)
        await page.wait_for_timeout(300)

        # ── Step 3: Click Aceptar (province) → goes to selectSede ──────────────
        await page.click("#btnAceptar")
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_selector("select#sede", timeout=20000)

        # ── Step 4: Select office ─────────────────────────────────────────────
        sede_opts = await page.query_selector_all("select#sede option")
        office_value = None
        for opt in sede_opts:
            val  = await opt.get_attribute("value") or ""
            text = (await opt.inner_text()).strip()
            if office_name.lower() in text.lower():
                office_value = val; break

        if not office_value:
            opts_dbg = [(await o.get_attribute("value"), (await o.inner_text()).strip()) for o in sede_opts]
            log.error(f"  Office '{office_name}' not found. Available: {opts_dbg}")
            return None

        log.info(f"  Office: {office_value}")
        await page.select_option("select#sede", value=office_value)
        await page.wait_for_timeout(500)

        # ── Step 5: Select tramite from select#tramiteGrupo[0] ────────────────
        # Tramite select appears on same page after office selection (no page reload)
        tramite_sel = await page.query_selector("select[id^='tramiteGrupo']")
        if not tramite_sel:
            log.error("  Tramite select not found")
            log.error(f"  Page: {(await page.inner_text('body'))[:300]}")
            return None

        tramite_opts = await tramite_sel.query_selector_all("option")
        tramite_value = None
        tramite_label = None
        for opt in tramite_opts:
            val  = await opt.get_attribute("value") or ""
            text = (await opt.inner_text()).strip().upper()
            if tramite_kw in text:
                tramite_value = val
                tramite_label = text
                break

        if not tramite_value:
            opts_dbg = [(await o.get_attribute("value"), (await o.inner_text()).strip()) for o in tramite_opts]
            log.error(f"  Tramite '{tramite_kw}' not found. Available: {opts_dbg}")
            return None

        log.info(f"  Tramite: {tramite_label} (value={tramite_value})")
        tramite_sel_id = await tramite_sel.get_attribute("id")
        await page.select_option(f"#{tramite_sel_id}", value=tramite_value)
        await page.wait_for_timeout(500)

        # ── Step 6: Click Aceptar → result page ──────────────────────────────
        await page.click("#btnAceptar")
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(1000)

        # ── Step 7: Extract result ────────────────────────────────────────────
        result = await extract_result(page)
        log.info(f"  Result: {result}")
        return result

    except Exception as e:
        log.error(f"  Scraping error: {e}")
        return None


async def extract_result(page) -> str:
    for sel in ["#citaNoDisponible", ".mf-msg__info", ".mf-msg__exito",
                ".mf-msg__error", "#mensajeInfo", "#msgError", "#inicio"]:
        el = await page.query_selector(sel)
        if el:
            text = (await el.inner_text()).strip()
            if text and len(text) > 5:
                return text

    body = await page.inner_text("body")
    keywords = ["lote", "expediente", "cita", "disponible", "no hay", "turno"]
    lines = [l.strip() for l in body.split("\n")
             if any(k in l.lower() for k in keywords) and l.strip()]
    if lines:
        return " | ".join(lines[:3])

    chunks = body.split()
    return " ".join(chunks[:20]) if chunks else "No text"

# ── Notification ──────────────────────────────────────────────────────────────
def build_notification(target: dict, old_text: str | None, new_text: str) -> str:
    now      = datetime.now().strftime("%d.%m.%Y %H:%M")
    expected = target.get("expected_lot", "").strip()
    highlight = ""
    if expected:
        if expected.lower() in new_text.lower():
            highlight = "🎯 <b>ВАШ ЛОТ НАЙДЕН!</b>\n"
        else:
            for n in re.findall(r'\d+', new_text):
                if expected.isdigit() and int(n) >= int(expected):
                    highlight = f"⚡️ <b>Лот {n} ≥ ожидаемого ({expected})!</b>\n"
                    break
    change = f"📌 <i>Было:</i> {old_text}\n" if old_text else ""
    return (
        f"{highlight}"
        f"🔔 <b>Изменение на ICP!</b>\n"
        f"📍 {target['province_name']} → {target['office_name']}\n"
        f"📋 {target['tramite']}\n\n"
        f"{change}"
        f"✅ <b>Стало:</b> {new_text}\n\n"
        f"🕐 {now}\n"
        f"🔗 <a href='{START_URL}'>Открыть сайт</a>"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    watchlist = json.loads(WATCHLIST_JSON)
    if not watchlist:
        log.warning("WATCHLIST_JSON is empty")
        return

    state = load_state()
    log.info(f"Loaded state with {len(state)} entries. Checking {len(watchlist)} targets...")

    async with async_playwright() as p:
        # Try system Chrome first (bypasses FortiGate), fall back to Chromium
        try:
            browser = await p.chromium.launch(
                channel="chrome",   # uses /Applications/Google Chrome.app
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            log.info("Using system Chrome")
        except Exception as e:
            log.warning(f"System Chrome not found ({e}), falling back to Chromium")
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            locale="es-ES",
            viewport={"width": 1280, "height": 800},
        )
        # Hide webdriver flag
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        for target in watchlist:
            key  = state_key(target)
            page = await context.new_page()
            try:
                new_text = await scrape_lot(page, target)
            finally:
                await page.close()

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

        await browser.close()

    log.info("Done.")

if __name__ == "__main__":
    asyncio.run(main())
