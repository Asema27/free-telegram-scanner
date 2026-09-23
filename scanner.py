import json
import os
from pathlib import Path
from datetime import datetime, timezone
from itertools import combinations

import ccxt
import requests

MIN_SPREAD_PERCENT = 3.0
ORDER_BOOK_LIMIT = 5
STATE_FILE = Path(os.environ.get("STATE_FILE", "scanner-state.json"))
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "AUTO")

# Binance can be blocked from some GitHub runner locations. The scanner
# continues with whichever public futures exchanges are reachable.
EXCHANGES = {
    "Binance": ccxt.binanceusdm({"enableRateLimit": True}),
    "Bybit": ccxt.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }),
    "OKX": ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }),
    "Bitget": ccxt.bitget({
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


def load_markets():
    available = {}
    for name, exchange in EXCHANGES.items():
        try:
            markets = exchange.load_markets()
            by_base = {}
            for symbol, market in markets.items():
                if (
                    market.get("active")
                    and market.get("swap")
                    and market.get("linear")
                    and market.get("quote") == "USDT"
                    and market.get("settle") == "USDT"
                ):
                    by_base[market.get("base")] = symbol
            if by_base:
                available[name] = by_base
                print(f"{name}: {len(by_base)} активных USDT-перпетуалов")
            else:
                print(f"{name}: подходящие контракты не найдены")
        except Exception as error:
            print(f"{name}: недоступна, пропускаю ({error})")
    return available


def fetch_quotes(available):
    quotes = {}
    for name, exchange in EXCHANGES.items():
        if name not in available:
            continue
        try:
            tickers = exchange.fetch_tickers()
            quotes[name] = {}
            for base, symbol in available[name].items():
                ticker = tickers.get(symbol, {})
                bid, ask = ticker.get("bid"), ticker.get("ask")
                if bid and ask and bid > 0 and ask > 0:
                    quotes[name][base] = {
                        "symbol": symbol,
                        "bid": bid,
                        "ask": ask,
                    }
        except Exception as error:
            print(f"{name}: не удалось получить котировки, пропускаю ({error})")
    return quotes


def find_candidate(base, venues):
    choices = []
    for buy_name, sell_name in combinations(venues, 2):
        for buyer, seller in ((buy_name, sell_name), (sell_name, buy_name)):
            buy = venues[buyer]
            sell = venues[seller]
            gross = (sell["bid"] / buy["ask"] - 1) * 100
            if gross >= MIN_SPREAD_PERCENT:
                choices.append((gross, buyer, seller, buy, sell))

    if not choices:
        return None
    gross, buy_name, sell_name, buy, sell = max(choices, key=lambda row: row[0])
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
    available = load_markets()
    if len(available) < 2:
        raise RuntimeError(
            "Доступно меньше двух бирж. Проверьте журнал запуска GitHub Actions."
        )

    common_bases = set.intersection(*(set(markets) for markets in available.values()))
    print(f"Общих монет минимум на двух биржах: {len(common_bases)}")
    quotes = fetch_quotes(available)
    previous_signals = read_state()
    current_signals = {}
    new_count = 0
    candidate_count = 0

    for base in sorted(common_bases):
        venues = {
            name: exchange_quotes[base]
            for name, exchange_quotes in quotes.items()
            if base in exchange_quotes
        }
        if len(venues) < 2:
            continue

        try:
            signal = find_candidate(base, venues)
            if not signal:
                continue
            if not confirm_with_order_books(signal):
                continue

            candidate_count += 1
            key = f"{base}:{signal['buy_exchange']}:{signal['sell_exchange']}"
            if key in previous_signals:
                current_signals[key] = round(signal["gross"], 4)
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
            try:
                telegram_send(message)
                current_signals[key] = round(signal["gross"], 4)
                new_count += 1
                print(f"Отправлен сигнал: {base}, {signal['gross']:.2f}%")
            except Exception as error:
                print(f"Не удалось отправить сигнал {base} в Telegram: {error}")
        except Exception as error:
            print(f"Пропуск {base}: {error}")

    STATE_FILE.write_text(
        json.dumps(current_signals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Готово. Подтверждённых кандидатов от {MIN_SPREAD_PERCENT}%: "
        f"{candidate_count}; новых уведомлений: {new_count}."
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        for exchange in EXCHANGES.values():
            exchange.close()
