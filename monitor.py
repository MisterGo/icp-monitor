#!/usr/bin/env python3
"""
ICP Appointment Monitor
Monitors https://icp.administracionelectronica.gob.es for lot number changes
and sends Telegram notifications.
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Watchlist: JSON string with list of targets
# Example:
# [
#   {"province": "33", "province_name": "Asturias",
#    "office": "12345", "office_name": "Oviedo",
#    "tramite": "POLICIA-RECOGIDA", "expected_lot": "150"},
#   ...
# ]
WATCHLIST_JSON = os.environ.get("WATCHLIST_JSON", "[]")

# State file path (persisted via GitHub Actions cache/artifact)
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/state.json"))

ICP_URL = "https://icp.administracionelectronica.gob.es/icpplus/index.html"

# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str):
    """Send a message via Telegram Bot API."""
    import urllib.request
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log.error(f"Telegram error: {result}")
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")

# ── State management ──────────────────────────────────────────────────────────
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

def state_key(target: dict) -> str:
    return f"{target['province']}_{target['office']}_{target['tramite']}"

# ── Scraper ───────────────────────────────────────────────────────────────────
async def scrape_lot(page, target: dict) -> str | None:
    """
    Navigate ICP site and return the lot/status text found on the result page.
    Returns None on failure.
    """
    try:
        log.info(f"Checking: {target['province_name']} / {target['office_name']} / {target['tramite']}")

        # Step 1: Load main page
        await page.goto(ICP_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector("select#form", timeout=15000)

        # Step 2: Select province by matching p=CODE in the option value URL
        province_code = target["province"]
        province_name = target["province_name"]
        options = await page.query_selector_all("select#form option")
        province_value = None
        for opt in options:
            val = await opt.get_attribute("value") or ""
            text = (await opt.inner_text()).strip()
            if f"p={province_code}&" in val or f"p={province_code}" in val:
                province_value = val
                break
            if province_name.lower() in text.lower():
                province_value = val
                break
        if not province_value:
            log.error(f"  Province '{province_name}' (code {province_code}) not found")
            opts_debug = [(await o.get_attribute("value"), (await o.inner_text()).strip()) for o in options]
            log.error(f"  Available: {opts_debug}")
            return None
        log.info(f"  Province value: {province_value}")
        await page.select_option("select#form", value=province_value)
        await page.wait_for_timeout(500)

        # Step 3: Click Aceptar — it's type=button with id=btnAceptar, not type=submit
        await page.click("#btnAceptar")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # Step 4: Select office by matching office_name text or office code in value
        await page.wait_for_selector("select#sede", timeout=15000)
        office_name = target["office_name"]
        office_code = str(target["office"])
        sede_options = await page.query_selector_all("select#sede option")
        office_value = None
        for opt in sede_options:
            val = await opt.get_attribute("value") or ""
            text = (await opt.inner_text()).strip()
            if office_code and office_code in val:
                office_value = val
                break
            if office_name.lower() in text.lower():
                office_value = val
                break
        if not office_value:
            log.error(f"  Office '{office_name}' not found")
            opts_debug = [(await o.get_attribute("value"), (await o.inner_text()).strip()) for o in sede_options]
            log.error(f"  Available offices: {opts_debug}")
            return None
        log.info(f"  Office value: {office_value}")
        await page.select_option("select#sede", value=office_value)
        await page.wait_for_timeout(500)

        # Step 5: Click Aceptar again
        await page.click("#btnAceptar")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1000)

        # Step 6: Select tramite — try radio button first, then select
        tramite_keyword = target["tramite"].upper()
        
        # Try radio buttons
        radios = await page.query_selector_all("input[type=radio]")
        found_radio = False
        for radio in radios:
            label_for = await radio.get_attribute("id")
            # Look for associated label text
            label = await page.query_selector(f"label[for='{label_for}']")
            label_text = (await label.inner_text()).upper() if label else ""
            value = (await radio.get_attribute("value") or "").upper()
            if tramite_keyword in label_text or tramite_keyword in value:
                await radio.click()
                found_radio = True
                log.info(f"  Selected tramite via radio: {label_text or value}")
                break

        if not found_radio:
            # Try select element
            selects = await page.query_selector_all("select")
            for sel in selects:
                options = await sel.query_selector_all("option")
                for opt in options:
                    opt_text = (await opt.inner_text()).upper()
                    opt_val = (await opt.get_attribute("value") or "").upper()
                    if tramite_keyword in opt_text or tramite_keyword in opt_val:
                        await sel.select_option(value=await opt.get_attribute("value"))
                        log.info(f"  Selected tramite via select: {opt_text}")
                        found_radio = True
                        break
                if found_radio:
                    break

        if not found_radio:
            log.warning(f"  Tramite '{tramite_keyword}' not found on page")
            # Dump available options for debugging
            body = await page.inner_text("body")
            log.debug(f"  Page body: {body[:500]}")
            return None

        # Step 7: Submit tramite selection
        await page.wait_for_timeout(300)
        submit_btns = await page.query_selector_all("input[type=submit][value='Aceptar']")
        if submit_btns:
            await submit_btns[-1].click()
        else:
            await page.click("button[type=submit]")
        
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        # Step 8: Extract lot number / status text from result page
        result_text = await extract_lot_text(page)
        log.info(f"  Result: {result_text}")
        return result_text

    except Exception as e:
        log.error(f"  Scraping error for {target.get('office_name')}: {e}")
        return None


async def extract_lot_text(page) -> str:
    """
    Try multiple selectors to find the lot number / availability text.
    The ICP site shows text like:
      - "No hay citas disponibles" (no slots)
      - A lot/expediente number in a highlighted div
    """
    # Try known CSS patterns for the result page
    selectors_to_try = [
        "#citaNoDisponible",
        ".mf-msg__info",
        ".mf-msg__exito",
        ".mf-msg__error",
        "#mensajeInfo",
        "#msgError",
        "div.col-sm-12 p",
        "div#inicio",
    ]
    
    for sel in selectors_to_try:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) > 5:
                    return text
        except Exception:
            pass

    # Fallback: get all visible text and look for lot/cita keywords
    try:
        body = await page.inner_text("body")
        # Look for lines containing lot-related keywords
        keywords = ["lote", "expediente", "cita", "disponible", "número", "no hay"]
        lines = body.split("\n")
        relevant = [l.strip() for l in lines 
                    if any(kw in l.lower() for kw in keywords) and l.strip()]
        if relevant:
            return " | ".join(relevant[:3])
        # Return first non-empty chunk
        non_empty = [l.strip() for l in lines if l.strip() and len(l.strip()) > 10]
        return non_empty[0] if non_empty else "No text found"
    except Exception as e:
        return f"Error extracting text: {e}"


# ── Notification logic ────────────────────────────────────────────────────────
def build_notification(target: dict, old_text: str | None, new_text: str) -> str:
    """Build a Telegram notification message."""
    province = target["province_name"]
    office = target["office_name"]
    tramite = target["tramite"]
    expected_lot = target.get("expected_lot", "").strip()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Detect if expected lot is mentioned
    highlight = ""
    if expected_lot and expected_lot.lower() in new_text.lower():
        highlight = "🎯 <b>ВАШ ЛОТ НАЙДЕН!</b>\n"
    elif expected_lot:
        # Try to extract numeric lot from text and compare
        import re
        numbers_in_text = re.findall(r'\d+', new_text)
        for num in numbers_in_text:
            if int(num) >= int(expected_lot) if expected_lot.isdigit() else False:
                highlight = f"⚡️ <b>Лот {num} ≥ ожидаемого ({expected_lot})!</b>\n"
                break

    change_line = ""
    if old_text:
        change_line = f"📌 <i>Было:</i> {old_text}\n"

    msg = (
        f"{highlight}"
        f"🔔 <b>Изменение на ICP!</b>\n"
        f"📍 {province} → {office}\n"
        f"📋 {tramite}\n"
        f"\n"
        f"{change_line}"
        f"✅ <b>Стало:</b> {new_text}\n"
        f"\n"
        f"🕐 {now}\n"
        f"🔗 <a href='{ICP_URL}'>Открыть сайт</a>"
    )
    return msg


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    watchlist = json.loads(WATCHLIST_JSON)
    if not watchlist:
        log.warning("WATCHLIST_JSON is empty — nothing to monitor.")
        return

    state = load_state()
    log.info(f"Loaded state with {len(state)} entries. Checking {len(watchlist)} targets...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="es-ES",
            viewport={"width": 1280, "height": 800}
        )

        for target in watchlist:
            key = state_key(target)
            page = await context.new_page()
            
            try:
                new_text = await scrape_lot(page, target)
            finally:
                await page.close()

            if new_text is None:
                log.warning(f"  Could not get text for {key}, skipping state update")
                continue

            old_text = state.get(key)

            if new_text != old_text:
                log.info(f"  CHANGE DETECTED for {key}")
                log.info(f"    Old: {old_text}")
                log.info(f"    New: {new_text}")
                msg = build_notification(target, old_text, new_text)
                await send_telegram(msg)
                state[key] = new_text
                save_state(state)
            else:
                log.info(f"  No change for {key}")

        await browser.close()

    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
