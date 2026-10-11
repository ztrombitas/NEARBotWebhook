import os
import time
import logging
import threading
from datetime import datetime, timezone

from flask import Flask, request
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app_NEAR")

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

SYMBOL = "NEARUSDT"

# Validált dev0.9x_w1.4x 6-szintes DCA-létra (súly% / deviáció%) - NEARUSDT, TF=10 perc, max.szim=15
# (2026-09-27-i ÚJ VÉGLEGES beállítás, METODOLOGIA.md §6.8 - NEAR_vegleges_beallitas_20260927.xlsx;
#  a korábbi dev0.7x_w1.2x / max.szim=5 verziót váltja, amely a friss adaton likvidációt mutatott)
DCA_LEVELS = [
    (0.27, 0.00),
    (0.79, -6.64),
    (2.36, -13.50),
    (7.13, -20.42),
    (21.78, -27.35),
    (67.67, -34.21),
]

TP_PCT = 1.2578
TRIGGER_PCT = 1.0062  # nyers érték - a Trigger mezőt a Cornix NEM osztja le a leverage-dzsel
LEVERAGE = 20
TRAILING_PCT_RAW = 0.5031  # nyers TP*0.4, kerekítés nélkül
TRAILING_PCT = round(TRAILING_PCT_RAW * LEVERAGE, 4)  # = 10.062 - Cornix mezőbe beírandó érték:
# a Cornix a Personal Leveraged Trailing Percentet elosztja a trade tényleges
# leverage-ével, és a végeredményt sosem engedi 0.2% alá (Cornix support
# hivatalos válasza, 2026.08.17) - ezért itt a nyers effektív érték helyett
# a nyers*leverage értéket kell beírni/küldeni, hogy az osztás után a kívánt
# effektív Trailing% jöjjön ki

# FIGYELEM - MINIMUM RENDELÉS (2026.10.01-i "Hit profit before entry" tanulság): a Bitget egész
# NEAR-re kerekít lefelé, és min. 5 USDT kell. Az 1. szint notionalja = trade méret x 20 x 0.27%
# = 0.054 x trade méret. 5 USD alatti NEAR-árnál 2 NEAR kell -> trade méret >= ~186 USDT.

EXCHANGES_LIST = "Bitget Futures"
LEVERAGE_LINE = "Cross x20"

# ----------------------------------------------------------------------------
# BEÉPÍTETT ÜTEMEZŐ (2026.10.11, az app_ONDO.py-ban élesben bevált szerkezet) - TradingView alert NÉLKÜL működik.
# A script maga figyeli az órát: minden TIMEFRAME_MIN perces, órához kötött
# határon (:00, :10, :20, :30, :40, :50) lekéri a Bitget élő last price-át, ebből
# számolja az entry-létrát és a TP-t, majd kiküldi a jelet a Telegram-csatornára.
# Az ütemezés az órához (epoch) van kötve, NEM a program indulásához - így egy
# újraindulás/deploy nem csúsztatja el a ritmust.
# FONTOS: a gunicornt EGYETLEN workerrel kell indítani, különben minden worker
# saját ütemezőt futtatna és duplán mennének ki a jelek:
#     gunicorn app_NEAR:app --workers 1 --threads 2
# ----------------------------------------------------------------------------
TIMEFRAME_MIN = 10
SCHEDULE_OFFSET_SEC = int(os.environ.get("SCHEDULE_OFFSET_SEC", "45"))  # ennyivel a határ után küld
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1") != "0"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # opcionális: ha be van állítva, a /webhook csak ezzel hívható

BITGET_TICKER_URL = "https://api.bitget.com/api/v2/mix/market/ticker"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"
BITGET_TIMEOUT_SEC = 3
BITGET_RETRIES = 3          # ennyiszer próbálkozik egy jelnél
BITGET_RETRY_WAIT_SEC = 2   # két próbálkozás között

_state_lock = threading.Lock()
_state = {
    "scheduler_started": False,
    "last_boundary": None,      # az utoljára kiszolgált határ (epoch mp) - duplázás ellen
    "last_signal_utc": None,
    "last_price": None,
    "last_result": None,
    "signals_sent": 0,
    "signals_skipped": 0,
    "next_run_utc": None,
}


def _utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_bitget_last_price(symbol):
    """A Bitget USDT-perpetual aktuális last price-a; többször próbálkozik, hiba esetén None."""
    for attempt in range(1, BITGET_RETRIES + 1):
        try:
            resp = requests.get(
                BITGET_TICKER_URL,
                params={"symbol": symbol, "productType": BITGET_PRODUCT_TYPE},
                timeout=BITGET_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") == "00000" and payload.get("data"):
                price = float(payload["data"][0]["lastPr"])
                if price > 0:
                    return price
            logger.warning("Bitget ticker váratlan válasz (%s. próba): %s", attempt, payload)
        except Exception as exc:  # noqa: BLE001 - hiba esetén újrapróbálunk, végül kihagyjuk a jelet
            logger.warning("Bitget ticker lekérési hiba (%s, %s. próba): %s", symbol, attempt, exc)
        if attempt < BITGET_RETRIES:
            time.sleep(BITGET_RETRY_WAIT_SEC)
    return None


def send_telegram_message(text):
    """Elküldi a jelet a Telegram-csatornára; True, ha sikerült."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in (1, 2):
        try:
            resp = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID, "text": text}, timeout=10)
            if resp.ok:
                logger.info("Telegram üzenet elküldve.")
                return True
            # a Telegram a hiba pontos okát a válasz "description" mezőjében adja meg
            # (pl. "chat not found", "need administrator rights in the channel chat")
            try:
                description = resp.json().get("description", resp.text[:200])
            except ValueError:
                description = resp.text[:200]
            logger.warning("Telegram küldési hiba (%s. próba): HTTP %s - %s | chat_id: %s",
                           attempt, resp.status_code, description, TELEGRAM_CHANNEL_ID)
            if resp.status_code in (400, 401, 403, 404):
                return False  # beállítási hiba (csatorna-azonosító / jogosultság / token) - újrapróbálni felesleges
        except Exception as exc:  # noqa: BLE001 - szándékosan széles, csak logolunk
            # a kivétel szövege tartalmazhatja az URL-t a bot tokennel - ezt kitakarjuk a logból
            logger.warning("Telegram küldési hiba (%s. próba): %s", attempt,
                           str(exc).replace(TELEGRAM_BOT_TOKEN, "***"))
        time.sleep(1)
    return False


def build_signal_message(entry_price, symbol=SYMBOL):
    entry_lines = []
    for i, (weight_pct, dev_pct) in enumerate(DCA_LEVELS, start=1):
        price = round(entry_price * (1 + dev_pct / 100), 6)
        entry_lines.append(f"{i}) {price}")

    # a formális Stop: kulcsszó a Cornix parser miatt kell (a tényleges védelmet
    # a DCA-létra + margin-kezelés adja, nem ez a stop)
    safety_stop = round(entry_price * (1 + DCA_LEVELS[-1][1] / 100) * 0.95, 6)

    # Explicit TP-ár a nyers Signal-objektumnak, hogy a Cornix háttérrendszere
    # be nem töltött ("waiting") jelek esetén is tudja auto-zárni a jelet, amint
    # az ár eléri - anélkül ezek a sosem-nyílt jelek örökre "Open" állapotban
    # maradnak a csatorna nyitott-jel limitje (Number of Open Signals per Channel)
    # ellen. A te tényleges trade-ed TP-je továbbra is a Cornix boton belüli
    # "Personal" beállításból (First Take-Profit Distance) számolódik - ez a sor
    # csak a jel saját záró-referenciája.
    tp_price = round(entry_price * (1 + TP_PCT / 100), 6)

    return f"""#{symbol}

Exchanges: {EXCHANGES_LIST}

Leverage: {LEVERAGE_LINE}

Entry Targets:
{chr(10).join(entry_lines)}

Take-Profit:
1) {tp_price}

Stop Targets:
1) {safety_stop}

Trailing Configuration:
Stop: Percent Below Highest ({TRAILING_PCT}%) - Trigger: Percent ({TRIGGER_PCT}%)
"""


def run_signal(source, fallback_price=None):
    """Egy jel teljes folyamata: Bitget-ár -> üzenet -> Telegram. Visszaadja az eredményt."""
    price = get_bitget_last_price(SYMBOL)
    price_source = "bitget"
    if price is None and fallback_price:
        price, price_source = float(fallback_price), "kézi (fallback)"

    now_utc = _utc(time.time())
    if price is None:
        logger.warning("JEL KIHAGYVA %s | forrás: %s | ok: a Bitget nem adott árat", SYMBOL, source)
        with _state_lock:
            _state["signals_skipped"] += 1
            _state["last_result"] = f"kihagyva (nincs Bitget-ár) - {now_utc}"
        return {"status": "skipped", "reason": "no_bitget_price"}

    ok = send_telegram_message(build_signal_message(price))
    logger.info("JEL %s | indító: %s | ár: %s (%s) | Telegram: %s",
                SYMBOL, source, price, price_source, "OK" if ok else "HIBA")
    with _state_lock:
        _state["last_signal_utc"] = now_utc
        _state["last_price"] = price
        if ok:
            _state["signals_sent"] += 1
            _state["last_result"] = f"elküldve - {now_utc}"
        else:
            _state["signals_skipped"] += 1
            _state["last_result"] = f"Telegram-hiba - {now_utc}"
    return {"status": "sent" if ok else "telegram_error", "price_source": price_source, "entry_price": price}


def _next_boundary(now_ts):
    """A következő, órához (epoch) kötött TIMEFRAME_MIN perces határ epoch-időbélyege."""
    period = TIMEFRAME_MIN * 60
    return (int(now_ts) // period + 1) * period


def _scheduler_loop():
    logger.info("ÜTEMEZŐ elindult | %s | TF=%s perc | eltolás=%s mp", SYMBOL, TIMEFRAME_MIN, SCHEDULE_OFFSET_SEC)
    while True:
        try:
            boundary = _next_boundary(time.time())
            target = boundary + SCHEDULE_OFFSET_SEC
            with _state_lock:
                _state["next_run_utc"] = _utc(target)
            while True:
                remaining = target - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 20))
            with _state_lock:
                if _state["last_boundary"] == boundary:
                    continue  # ezt a határt már kiszolgáltuk
                _state["last_boundary"] = boundary
            run_signal("ütemező")
        except Exception as exc:  # noqa: BLE001 - az ütemező szál soha nem állhat le
            logger.exception("Ütemező-hiba: %s", exc)
            time.sleep(5)


def start_scheduler():
    with _state_lock:
        if _state["scheduler_started"]:
            return
        _state["scheduler_started"] = True
    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()


@app.route("/webhook", methods=["POST"])
def webhook():
    """Kézi/tartalék indítás (pl. teszthez). Az ár megadása nem kötelező: a jel a Bitget-árból készül."""
    data = request.get_json(force=True, silent=True) or {}
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        return {"status": "forbidden"}, 403
    result = run_signal("webhook", fallback_price=data.get("price"))
    return result, 200 if result["status"] == "sent" else 503


@app.route("/", methods=["GET"])
def health():
    with _state_lock:
        state = dict(_state)
    return {
        "status": "ok",
        "symbol": SYMBOL,
        "timeframe_min": TIMEFRAME_MIN,
        "scheduler_enabled": SCHEDULER_ENABLED,
        "scheduler_running": state["scheduler_started"],
        "next_run_utc": state["next_run_utc"],
        "last_signal_utc": state["last_signal_utc"],
        "last_price": state["last_price"],
        "last_result": state["last_result"],
        "signals_sent": state["signals_sent"],
        "signals_skipped": state["signals_skipped"],
        "tp_pct": TP_PCT,
        "trigger_pct": TRIGGER_PCT,
        "trailing_pct_cornix_field": TRAILING_PCT,
    }, 200


if SCHEDULER_ENABLED:
    start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
