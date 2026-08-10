import os

from flask import Flask, request
import requests

app = Flask(__name__)

# A tokent és a csatorna-ID-t NEM a kódba írjuk (publikus repo lesz),
# hanem a render.com Dashboardon, Environment Variables alatt adjuk meg.
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

# NEAR/USDT végleges, validált DCA-létra (dev0.7x_w1.2x variáns)
# Deviáció% az aktuális (jelzéskori) árhoz képest, szintenként
DCA_DEVIATIONS = [
    0.0000,   # Target 1
    -0.0517,  # Target 2
    -0.1050,  # Target 3
    -0.1588,  # Target 4
    -0.2127,  # Target 5
    -0.2661,  # Target 6
]

# A Stop csak formai kötelezettség a Cornix parserének (a bot saját Stop
# funkciója Off-on áll, a védelmet a DCA-létra + margin-kezelés adja) -
# ezért egy nominális ár kerül a legmélyebb szint alá, biztonsági ráhagyással
STOP_BUFFER_PCT = 0.05  # 5%-kal a 6. (legmélyebb) szint alá

EXCHANGES_LIST = "Binance Futures, Bybit USDT, OKX Futures, KuCoin Futures, Bitget Futures, Hyperliquid Futures, BingX Futures"
LEVERAGE_LINE = "Cross x20"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    entry_price = float(data["price"])
    symbol = data.get("symbol", "NEAR/USDT")

    # Mind a 6 DCA-szint konkrét árának kiszámítása a validált
    # deviációk alapján, a jelzéskori (TradingView-ból érkező) árhoz képest
    entry_prices = [
        round(entry_price * (1 + dev), 6) for dev in DCA_DEVIATIONS
    ]

    # Nominális, formai Stop a legmélyebb szint alatt (Cornix parser miatt
    # kötelező mező, de a bot Stop=Off állása miatt nem hoz létre tényleges
    # SL-rendelést belőle)
    deepest_level = entry_prices[-1]
    safety_stop = round(deepest_level * (1 - STOP_BUFFER_PCT), 6)

    entry_lines = "\n".join(
        f"{i+1}) {price}" for i, price in enumerate(entry_prices)
    )

    # Nincs Take-Profit és Trailing Configuration a szövegben, mert a
    # Cornix Signal Boton mindkettő Personal módban van beállítva
    # (First Take-Profit Distance: 1.53%, Leveraged Trailing: 0.6%/1.2%)
    # - ezeket a bot magától, a saját konfigurációja alapján alkalmazza.
    message = f"""#{symbol}

Exchanges: {EXCHANGES_LIST}

Leverage: {LEVERAGE_LINE}

Entry:
{entry_lines}

Stop Targets:
1) {safety_stop}
"""

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID, "text": message})

    return {"status": "sent", "telegram_response": resp.json()}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
