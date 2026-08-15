import os
from flask import Flask, request
import requests

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

# Validált dev0.7x_w1.2x DCA-létra (súly% / deviáció%) - NEARUSDT
DCA_LEVELS = [
    (0.54, 0.00),
    (1.37, -5.17),
    (3.50, -10.50),
    (9.02, -15.88),
    (23.49, -21.27),
    (62.08, -26.61),
]

TP_PCT = 1.2312
TRIGGER_PCT = 0.9850
TRAILING_PCT = 0.4925

EXCHANGES_LIST = "Binance Futures, Bybit USDT, OKX Futures, KuCoin Futures, Bitget Futures, Hyperliquid Futures, BingX Futures"
LEVERAGE_LINE = "Cross x20"


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

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID, "text": message})

    return {"status": "sent", "telegram_response": resp.json()}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
