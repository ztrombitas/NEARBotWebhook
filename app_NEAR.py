import os
import logging
import threading

from flask import Flask, request
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app_NEAR")

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

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
TRAILING_PCT_RAW = 0.5031  # kívánt effektív (nyers, TP*0.4) Trailing%
TRAILING_PCT = round(TRAILING_PCT_RAW * LEVERAGE, 4)  # = 10.062 - Cornix mezőbe beírandó érték:
# a Cornix a Personal Leveraged Trailing Percentet elosztja a trade tényleges
# leverage-ével, és a végeredményt sosem engedi 0.2% alá (Cornix support
# hivatalos válasza, 2026.08.17) - ezért itt a nyers effektív érték helyett
# a nyers*leverage értéket kell beírni/küldeni, hogy az osztás után a kívánt
# effektív Trailing% jöjjön ki

EXCHANGES_LIST = "Bitget Futures"
LEVERAGE_LINE = "Cross x20"


def send_telegram_message(text):
    """Háttérszálon fut (SOL/HBAR-javítás) - nem blokkolja a webhook válaszát."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID, "text": text}, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram üzenet elküldve.")
    except Exception as exc:  # noqa: BLE001 - szándékosan széles, csak logolunk
        logger.exception("Telegram küldési hiba: %s", exc)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    entry_price = float(data["price"])
    symbol = data.get("symbol", "NEARUSDT")

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

    message = f"""#{symbol}

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

    # a Telegram-küldés háttérszálra kerül, hogy a webhook azonnal visszatérjen -
    # így a TradingView webhook 3 másodperces timeout-ja nem tud hibát dobni még
    # akkor sem, ha a Telegram API épp lassabban válaszol
    threading.Thread(target=send_telegram_message, args=(message,), daemon=True).start()

    return {"status": "accepted"}, 200


@app.route("/", methods=["GET"])
def health():
    return {
        "status": "ok",
        "symbol": "NEARUSDT",
        "tp_pct": TP_PCT,
        "trigger_pct": TRIGGER_PCT,
        "trailing_pct_cornix_field": TRAILING_PCT,
    }, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
