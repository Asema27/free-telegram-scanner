import json
import os
from pathlib import Path
from datetime import datetime, timezone

import ccxt
import requests

MIN_SPREAD_PERCENT = 3.0
ORDER_BOOK_LIMIT = 5
STATE_FILE = Path(os.environ.get("STATE_FILE", "scanner-state.json"))
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "AUTO")

EXCHANGES = {
    "Binance": ccxt.binanceusdm({"enableRateLimit": True}),
    "Bybit": ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }),
}


def telegram_send(message):
    global CHAT_ID
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    if CHAT_ID == "AUTO":
        response = requests.get(f"{base}/getUpdates", timeout=20)
        response.raise_for_status()
        for update in reversed(response.json().get("result", [])):
            incoming = update.get("message") or update.get("edited_message")
            chat = incoming.get("chat") if incoming else None
            if chat and chat.get("type") == "private":
                CHAT_ID = str(chat["id"])
                break
        if CHAT_ID == "AUTO":
            raise RuntimeError(
                "Chat ID не найден. Откройте бота в Telegram, нажмите Start и отправьте /start."
            )

    response = requests.post(
        f"{base}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram вернул ошибку: {result}")


def load_common_markets():
    market_sets = {}
    for name, exchange in EXCHANGES.items():
        markets = exchange.load_markets()
        market_sets[name] = {
            symbol: market
            for symbol, market in markets.items()
            if market.get("active")
            and market.get("swap")
            and market.get("linear")
            and market.get("quote") == "USDT"
            and market.get("settle") == "USDT"
        }
        print(f"{name}: {len(market_sets[name])} активных USDT-перпетуалов")

    bybit_by_contract = {}
    for symbol, market in market_sets["Bybit"].items():
        key = (market.get("baseId"), market.get("quoteId"), market.get("contractSize"))
        bybit_by_contract[key] = symbol

    pairs = []
    for binance_symbol, market in market_sets["Binance"].items():
        key = (market.get("baseId"), market.get("quoteId"), market.get("contractSize"))
        bybit_symbol = bybit_by_contract.get(key)
        if bybit_symbol:
            pairs.append((market.get("base"), binance_symbol, bybit_symbol))
    return pairs


def ticker_candidate(base, binance_symbol, bybit_symbol, tickers):
    quotes = {}
    for name, symbol in (("Binance", binance_symbol), ("Bybit", bybit_symbol)):
        ticker = tickers[name].get(symbol, {})
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask or bid <= 0 or ask <= 0:
            return None
        quotes[name] = {"symbol": symbol, "bid": bid, "ask": ask}

    options = []
    for buy_name, sell_name in (("Binance", "Bybit"), ("Bybit", "Binance")):
        buy, sell = quotes[buy_name], quotes[sell_name]
        gross = (sell["bid"] / buy["ask"] - 1) * 100
        options.append((gross, buy_name, sell_name, buy, sell))

    gross, buy_name, sell_name, buy, sell = max(options, key=lambda row: row[0])
    if gross < MIN_SPREAD_PERCENT:
        return None

    return {
        "base": base,
        "buy_exchange": buy_name,
        "sell_exchange": sell_name,
        "buy_symbol": buy["symbol"],
        "sell_symbol": sell["symbol"],
        "gross": gross,
    }


def confirm_with_order_books(signal):
    buy_book = EXCHANGES[signal["buy_exchange"]].fetch_order_book(
        signal["buy_symbol"], ORDER_BOOK_LIMIT
    )
    sell_book = EXCHANGES[signal["sell_exchange"]].fetch_order_book(
        signal["sell_symbol"], ORDER_BOOK_LIMIT
    )
    if not buy_book["asks"] or not sell_book["bids"]:
        return False

    buy_ask, buy_size = buy_book["asks"][0]
    sell_bid, sell_size = sell_book["bids"][0]
    spread = (sell_bid / buy_ask - 1) * 100
    if spread < MIN_SPREAD_PERCENT:
        return False

    signal.update({
        "buy_ask": buy_ask,
        "sell_bid": sell_bid,
        "buy_size": buy_size,
        "sell_size": sell_size,
        "gross": spread,
    })
    return True


def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    pairs = load_common_markets()
    print(f"Совпадающих контрактов: {len(pairs)}")

    tickers = {
        name: exchange.fetch_tickers()
        for name, exchange in EXCHANGES.items()
    }

    previous_signals = read_state()
    current_signals = {}
    new_count = 0

    for base, binance_symbol, bybit_symbol in pairs:
        try:
            signal = ticker_candidate(
                base, binance_symbol, bybit_symbol, tickers
            )
            if not signal or not confirm_with_order_books(signal):
                continue

            key = f"{base}:{signal['buy_exchange']}:{signal['sell_exchange']}"
            current_signals[key] = round(signal["gross"], 4)
            if key in previous_signals:
                continue

            message = (
                f"⚠️ Кандидат на расхождение цен\n"
                f"Монета: {base}/USDT perpetual\n"
                f"Купить: {signal['buy_exchange']} по ask {signal['buy_ask']}\n"
                f"Продать: {signal['sell_exchange']} по bid {signal['sell_bid']}\n"
                f"Разница до комиссий: {signal['gross']:.2f}%\n"
                f"Объём верхнего уровня стакана: купить {signal['buy_size']}, "
                f"продать {signal['sell_size']}\n"
                f"Время UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Проверьте комиссии, funding, стакан и доступность контрактов вручную."
            )
            telegram_send(message)
            new_count += 1
            print(f"Отправлен сигнал: {base}, {signal['gross']:.2f}%")
        except Exception as error:
            print(f"Пропуск {base}: {error}")

    STATE_FILE.write_text(
        json.dumps(current_signals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Готово. Кандидатов от {MIN_SPREAD_PERCENT}%: {len(current_signals)}; "
        f"новых уведомлений: {new_count}."
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        for exchange in EXCHANGES.values():
            exchange.close()
