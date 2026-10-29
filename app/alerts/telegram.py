"""Telegram alerts. The bot token and chat id come from .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
or from the settings page. The token is never returned by any API."""
import asyncio

import httpx

from .. import config, settings_store
from ..db import SessionLocal

last_error: str | None = None
TF = {"1m": "1 min", "5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 hr", "2h": "2 hr", "4h": "4 hr", "1d": "Day",
      "1w": "Week", "1mo": "Month", "3mo": "Quarter", "6mo": "Half year", "1y": "Year"}


def p(value) -> str:
    """A price as the coin trades it: 8 significant digits, no trailing zeros."""
    return "none" if value is None else f"{float(value):.8g}"


def credentials() -> tuple[str, str]:
    with SessionLocal() as db:
        return (config.TELEGRAM_BOT_TOKEN or settings_store.get(db, "telegram_token") or "",
                str(config.TELEGRAM_CHAT_ID or settings_store.get(db, "telegram_chat_id") or ""))


def configured() -> bool:
    return all(credentials())


async def send(text: str) -> tuple[bool, str]:
    """Send one message. Returns (ok, detail); never raises."""
    global last_error
    token, chat = credentials()
    if not (token and chat):
        return False, "Telegram is not set up: add the bot token and chat id in settings."
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text})
        if r.status_code == 200 and r.json().get("ok"):
            last_error = None
            return True, "sent"
        last_error = f"Telegram refused the message: {r.status_code} {r.text[:150]}"
    except Exception as exc:
        last_error = f"Telegram could not be reached: {type(exc).__name__}"
    return False, last_error


def notify(text: str) -> None:
    """Fire and forget, from inside the running app. Does nothing if Telegram is not set up or alerts are off."""
    with SessionLocal() as db:
        if not settings_store.get(db, "telegram_alerts"):
            return
    if configured():
        asyncio.get_running_loop().create_task(send(text))


def alert_text(a: dict) -> str:
    kind = a.get("entry_type") or "TYPE_1"
    at_zone = "entry reached" if kind == "TYPE_1" else "price is at the zone"
    what = {"approaching": "approaching", "triggered": at_zone}.get(a["alert"], a["alert"])
    side = "long" if a["side"] == "demand" else "short"
    text = f"{a['symbol']} {TF.get(a['timeframe'], a['timeframe'])} {a['side']} zone ({side}): {what} at {p(a['price'])}\n"
    if kind == "TYPE_1":
        text += f"Entry type 1 | Entry {p(a['entry'])} | Stop {p(a['stop'])} | Target {p(a['target'])} | R:R {a['reward_risk']} | Score {a['score']:g}"
    else:
        need = "the next candle opens inside it" if kind == "TYPE_2" else "the next candle closes back out of it"
        text += f"Entry type {kind[-1]}: wait for a candle to close inside the zone, then {need} | Stop {p(a['stop'])} | Score {a['score']:g}"
    if a.get("mode"):
        text += f"\n{a['mode']} mode | Curve {a.get('curve') or 'unknown'} | ITF {a.get('itf_trend') or 'unknown'} | {a.get('label') or ''}".rstrip(" |")
    return text


def paper_text(e: dict) -> str:
    pnl = f" | P&L {e['pnl']:.2f} USDT" if e.get("pnl") is not None else ""
    side = "long" if e["side"] == "demand" else "short"
    return f"Paper trade {e['event']}: {e['instrument_key']} {side} {e['quantity']:g} at {p(e['price'])}{pnl}"
