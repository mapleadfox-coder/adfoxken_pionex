"""
Pionex 合約(PERP)技術條件監控 + Telegram 通知
=============================================

功能:
1. 取得 Pionex 目前支援的所有合約(PERP)幣種
2. 篩選出「24 小時成交金額(USDT) > 門檻」的幣種
3. 針對這些幣種抓取 5 分鐘 K 線,計算並判斷三個條件:
   條件一:該幣種 24 小時成交金額(USDT) > min_24h_amount_usdt
   條件二:最新收盤 K 線的成交量 > vol_multiplier 倍的 MAVOL5(近 5 根 K 線
           成交量的平均值,含本身),且該 K 線收陽(收盤價 > 開盤價)
   條件三:最新收盤 K 線漲跌幅絕對值 >= pct_change_multiplier 倍的「前一根
           K 線」漲跌幅絕對值;若前一根漲跌幅絕對值為 0,則改比較「前前一根」
4. 三個條件同時成立的幣種,整理成一則訊息透過 Telegram 發送通知

執行環境需要兩個環境變數(在 GitHub Actions 裡用 Secrets 設定):
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

PIONEX_BASE = "https://api.pionex.com"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

# 兩支機器人(訊號機器人 / 型態機器人)共用的非加密貨幣排除清單,存放在 pionex repo 裡。
# 每次執行都會嘗試從這裡即時抓取最新清單,失敗才退回使用本地 config.json 裡的備援清單。
SHARED_EXCLUDE_URL = "https://raw.githubusercontent.com/adfoxken-coder/pionex/main/shared_excluded_assets.json"

TAIPEI_TZ = timezone(timedelta(hours=8))

DEFAULT_CONFIG = {
    "min_24h_amount_usdt": 20000,    # 條件一:24 小時成交金額(USDT)門檻
    "mavol_period": 5,               # 條件二:MAVOL 的期數
    "vol_multiplier": 1.5,           # 條件二:成交量需超過 MAVOL 的倍數
    "pct_change_multiplier": 2.0,    # 條件三:漲跌幅需超過前一根的倍數
    "min_pct_change_1h": 1.0,        # 額外規則:1 小時級別的漲幅需 >= 這個百分比才推播(4H/1D 不受影響)
    "min_body_ratio": 0.7,           # 條件四:實體(收盤-開盤)需佔整根K線(高-低)的比例
    "max_prev_wick_ratio": 0.5,      # 條件五:前一根K線影線不能超過最新這根K線(高-低)的比例
    "kline_fetch_limit": 15,         # 每次抓取的 K 線根數(需 >= mavol_period + 3)
    "request_sleep_sec": 0.15,       # 每次呼叫 klines API 之間的間隔,避免超過速率限制
    "settle_delay_sec": 45,          # 排程一開始先等待幾秒,確保交易所該收盤的K線已經寫入完成

    # 只偵測加密貨幣,排除美股代幣(xStocks)、貴金屬等非加密貨幣資產。
    # 這份清單是根據公開資訊整理,不保證完整;發現漏網或誤殺歡迎手動增減。
    "excluded_base_currencies": [
        # 美股代幣(xStocks,Backed Finance 發行)
        "AAPLX", "TSLAX", "NVDAX", "SPYX", "QQQX", "MSTRX", "CRCLX", "GOOGLX",
        "VTIX", "BRK.BX", "UNHX", "GMEX", "CMCSAX", "PGX", "NFLXX", "XOMX",
        "AMBRX", "LLYX", "ABBVX", "VX", "CSCOX", "MCDX", "NVOX", "KRAQX",
        "PFEX", "INTCX", "HOODX", "AMZNX", "METAX", "COINX", "MSFTX",
        "TQQQX", "DFDVX", "ASMLX", "TSMX", "SNDKX", "USOX", "QNTX", "RGTIX",
        "QCOMX", "GLWX", "VVV", "SHAZX", "SMHX",
        "COHRX", "CRWVX", "FLNCX", "IRENX", "OKLOX", "PAYPX", "SMCIX", "ORCLX",
        # 美股/韓股代幣(使用者回報確認為股票代幣)
        "AAOIX", "AXTIX", "CXMTX", "DRAMX", "SKHX",
        # 私人公司/未上市股權相關代幣(使用者回報確認非加密貨幣)
        "OPENAI", "ANTHROPIC",
        # 原物料/大宗商品(石油等,非加密貨幣)
        "BRENTOIL",
        # 貴金屬
        "PPLTX", "XAU", "XAG", "XPT", "XPD", "PAXG", "XAUT",
    ],
    # 「基礎貨幣」本身就是穩定幣的合約(例如 USDT/TRY、USDT/BRL 這類外匯型合約),
    # 屬於「其他」類別而非一般加密貨幣方向性交易標的,一併排除
    "excluded_stablecoin_bases": [
        "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "PYUSD", "USDE",
    ],
    # 若幣種名稱(name 欄位)包含以下關鍵字,也會自動排除(不分大小寫)
    "exclude_name_keywords": [
        "stock", "xstock", "gold", "silver", "platinum", "palladium", "metal",
    ],
}

INTERVAL_MS = {
    "1M": 1 * 60 * 1000,
    "5M": 5 * 60 * 1000,
    "15M": 15 * 60 * 1000,
    "30M": 30 * 60 * 1000,
    "60M": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
    "8H": 8 * 60 * 60 * 1000,
    "12H": 12 * 60 * 60 * 1000,
    "1D": 24 * 60 * 60 * 1000,
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def get_perp_symbols():
    """取得目前 Pionex 支援的所有合約(PERP)幣種,回傳 {symbol: {"base": ..., "name": ...}}"""
    resp = requests.get(
        f"{PIONEX_BASE}/api/v1/common/symbols",
        params={"type": "PERP"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("result"):
        raise RuntimeError(f"Pionex symbols API error: {data}")
    return {
        s["symbol"]: {
            "base": s.get("baseCurrency", s["symbol"]),
            "name": s.get("name", ""),
        }
        for s in data["data"]["symbols"]
        if s.get("enable", True)
    }


def get_perp_tickers():
    """取得所有合約(PERP)幣種的 24 小時行情資料"""
    resp = requests.get(
        f"{PIONEX_BASE}/api/v1/market/tickers",
        params={"type": "PERP"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("result"):
        raise RuntimeError(f"Pionex tickers API error: {data}")
    return {t["symbol"]: t for t in data["data"]["tickers"]}


def get_klines(session, symbol, interval, limit):
    resp = session.get(
        f"{PIONEX_BASE}/api/v1/market/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("result"):
        return []
    return data["data"]["klines"]


def format_price(x):
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def send_telegram_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id_raw:
        print("[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,跳過推播,僅印出訊息:")
        print(text)
        return

    # TELEGRAM_CHAT_ID 可以用逗號分隔多個 Chat ID,例如 "111111,222222",
    # 這樣同一則訊息就會分別發送給每一個人
    chat_ids = [c.strip() for c in chat_id_raw.split(",") if c.strip()]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        body = {
            "chat_id": chat_id,
            "text": text[:4000],
        }
        r = requests.post(url, json=body, timeout=15)
        if r.status_code != 200:
            print(f"[錯誤] Telegram 推播失敗(chat_id={chat_id}): {r.status_code} {r.text}")
        else:
            print(f"[完成] Telegram 推播成功(chat_id={chat_id})")


def evaluate_symbol(klines, config, interval_ms, now_ms):
    """回傳 (是否符合條件, 最新收盤價, 最新漲跌幅%) 或 (False, None, None)"""
    if not klines:
        return False, None, None

    sorted_klines = sorted(klines, key=lambda k: k["time"])
    # 只保留「已經收盤」的 K 線(排除還在形成中的最新一根)
    closed = [k for k in sorted_klines if k["time"] + interval_ms <= now_ms]

    mavol_period = config["mavol_period"]
    # 需要:最新這根 + 前面 mavol_period 根(不含本身)才能算 MAVOL,另外還要有前前根
    if len(closed) < max(mavol_period + 1, 3):
        return False, None, None  # 資料不足,跳過

    latest = closed[-1]
    prev = closed[-2]
    prev_prev = closed[-3]

    latest_open = float(latest["open"])
    latest_close = float(latest["close"])
    latest_high = float(latest["high"])
    latest_low = float(latest["low"])
    latest_vol = float(latest["volume"])

    if latest_open == 0:
        return False, None, None

    is_bullish = latest_close > latest_open
    latest_pct = (latest_close - latest_open) / latest_open * 100

    # 條件二:成交量 > vol_multiplier 倍的 MAVOL(取「這根之前」的 mavol_period 根,不含本身)
    recent_for_mavol = closed[-(mavol_period + 1):-1]
    mavol = sum(float(k["volume"]) for k in recent_for_mavol) / mavol_period
    vol_ok = mavol > 0 and latest_vol > config["vol_multiplier"] * mavol

    # 條件三:漲跌幅絕對值 >= pct_change_multiplier 倍的前一根(前一根為 0 則比前前一根)
    def candle_pct(k):
        o = float(k["open"])
        c = float(k["close"])
        if o == 0:
            return 0.0
        return (c - o) / o * 100

    prev_pct = candle_pct(prev)
    if abs(prev_pct) == 0:
        baseline = abs(candle_pct(prev_prev))
    else:
        baseline = abs(prev_pct)
    pct_ok = abs(latest_pct) >= config["pct_change_multiplier"] * baseline

    # 條件四:飽滿陽K,實體(收盤-開盤)佔整根K線(最高-最低)的比例 >= min_body_ratio
    candle_range = latest_high - latest_low
    if candle_range > 0:
        body_ratio = (latest_close - latest_open) / candle_range
    else:
        body_ratio = 0.0
    body_ok = body_ratio >= config["min_body_ratio"]

    # 條件五:前一根 K 線的「上影線」不能超過最新這根 K 線「實體」的一半
    prev_high = float(prev["high"])
    prev_open = float(prev["open"])
    prev_close = float(prev["close"])
    prev_upper_wick = prev_high - max(prev_open, prev_close)
    latest_body = latest_close - latest_open  # 已知 is_bullish 為真時此值為正
    if latest_body > 0:
        wick_ok = prev_upper_wick <= config["max_prev_wick_ratio"] * latest_body
    else:
        wick_ok = False

    matched = is_bullish and vol_ok and pct_ok and body_ok and wick_ok
    return matched, latest_close, latest_pct


def get_shared_exclusions():
    """
    嘗試從共用檔案(SHARED_EXCLUDE_URL)即時抓取最新的排除清單。
    成功回傳 dict(包含 excluded_base_currencies / excluded_stablecoin_bases /
    exclude_name_keywords 三個 key),失敗回傳 None(呼叫端會退回使用本地清單)。
    """
    try:
        resp = requests.get(SHARED_EXCLUDE_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("共用排除清單格式不正確")
        return data
    except Exception as e:
        print(f"[警告] 無法取得共用排除清單,改用本地備援清單:{e}")
        return None


def is_excluded_asset(symbol_info, config):
    """判斷是否為要排除的非加密貨幣資產(美股代幣、貴金屬、外匯型合約等)"""
    base = symbol_info.get("base", "").upper()
    name = (symbol_info.get("name") or "").lower()

    excluded_bases = {b.upper() for b in config.get("excluded_base_currencies", [])}
    if base in excluded_bases:
        return True

    stablecoin_bases = {b.upper() for b in config.get("excluded_stablecoin_bases", [])}
    if base in stablecoin_bases:
        return True  # 基礎貨幣本身是穩定幣,通常是外匯型合約,非一般加密貨幣

    keywords = config.get("exclude_name_keywords", [])
    if name and any(kw.lower() in name for kw in keywords):
        return True

    return False


INTERVAL_LABELS = {
    "60M": {"full": "1小時級別", "short": "1h"},
    "4H": {"full": "4小時級別", "short": "4h"},
    "1D": {"full": "日線級別", "short": "1d"},
}

REFERENCE_SYMBOL = "BTC_USDT_PERP"  # 用來偵測「1小時/4小時/日線K線是否有新的一根收盤」的參考幣種


def get_latest_closed_candle_time(session, symbol, interval, now_ms):
    """回傳指定週期「最新一根已收盤K線」的開盤時間(ms),沒有資料則回傳 None"""
    interval_ms = INTERVAL_MS.get(interval, 15 * 60 * 1000)
    try:
        klines = get_klines(session, symbol, interval, limit=5)
    except Exception as e:
        print(f"[警告] 取得 {symbol} {interval} 參考K線失敗:{e}")
        return None
    sorted_klines = sorted(klines, key=lambda k: k["time"])
    closed = [k for k in sorted_klines if k["time"] + interval_ms <= now_ms]
    if not closed:
        return None
    return closed[-1]["time"]


def main():
    config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    # 補齊任何缺少的設定值(例如使用者只改了部分欄位)
    for k, v in DEFAULT_CONFIG.items():
        config.setdefault(k, v)

    # 嘗試用共用排除清單覆蓋本地清單,讓「訊號機器人」跟「型態機器人」共用同一份
    # 非加密貨幣排除清單(只要更新共用檔案,兩邊下次執行就會自動套用)
    shared = get_shared_exclusions()
    if shared:
        for key in ("excluded_base_currencies", "excluded_stablecoin_bases", "exclude_name_keywords"):
            if key in shared:
                config[key] = shared[key]
        print("已套用共用排除清單")
    else:
        print("改用本地 config.json 裡的排除清單")

    run_start_taipei = datetime.now(TAIPEI_TZ)

    # 排程一開始先等待,確保交易所該收盤的K線已經確實寫入完成,避免抓到舊的一根
    settle_delay = config.get("settle_delay_sec", 45)
    if settle_delay > 0:
        print(f"等待 {settle_delay} 秒讓交易所K線資料寫入完成...")
        time.sleep(settle_delay)

    now_ms = int(time.time() * 1000)

    state = load_json(STATE_FILE, {})

    # 判斷這次要偵測哪些週期:不依賴「現在幾點幾分」(因為 GitHub 排程常有幾分鐘延遲,
    # 用時鐘猜測不可靠),而是直接問 Pionex「1小時/4小時/日線K線,最新收盤那一根,是不是
    # 比上次記錄的更新」。只要真的有新的一根收盤,不管排程延遲多久都一定抓得到。
    # 三個週期都沒有新K線收盤時,這次就完全不用做事,直接跳過(節省 API 呼叫)。
    session = requests.Session()
    intervals = []

    latest_60m_time = get_latest_closed_candle_time(session, REFERENCE_SYMBOL, "60M", now_ms)
    prev_60m_time = state.get("last_60m_boundary_ms")
    due_60m = latest_60m_time is not None and (prev_60m_time is None or latest_60m_time > prev_60m_time)
    if due_60m:
        intervals.append("60M")

    latest_4h_time = get_latest_closed_candle_time(session, REFERENCE_SYMBOL, "4H", now_ms)
    prev_4h_time = state.get("last_4h_boundary_ms")
    due_4h = latest_4h_time is not None and (prev_4h_time is None or latest_4h_time > prev_4h_time)
    if due_4h:
        intervals.append("4H")

    latest_1d_time = get_latest_closed_candle_time(session, REFERENCE_SYMBOL, "1D", now_ms)
    prev_1d_time = state.get("last_1d_boundary_ms")
    due_1d = latest_1d_time is not None and (prev_1d_time is None or latest_1d_time > prev_1d_time)
    if due_1d:
        intervals.append("1D")

    print(f"本次執行時間點:{run_start_taipei.strftime('%Y-%m-%d %H:%M:%S')} UTC+8,本次偵測週期:{intervals}")

    if not intervals:
        print("這次沒有任何週期有新K線收盤,不需要偵測,本次提早結束。")
        state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, state)
        return

    symbols_map = get_perp_symbols()      # {symbol: {"base":..., "name":...}}
    tickers = get_perp_tickers()          # {symbol: ticker}

    # 先排除非加密貨幣資產(美股代幣、貴金屬等)
    crypto_only = {
        symbol: info
        for symbol, info in symbols_map.items()
        if not is_excluded_asset(info, config)
    }
    excluded_count = len(symbols_map) - len(crypto_only)
    print(f"排除非加密貨幣資產(美股代幣/貴金屬等)數量:{excluded_count} / {len(symbols_map)}")

    # 條件一:先用 24 小時成交金額篩選,減少後續 K 線 API 呼叫量(所有週期共用同一份候選清單)
    candidates = []
    for symbol, info in crypto_only.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        try:
            amount_24h = float(ticker.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if amount_24h > config["min_24h_amount_usdt"]:
            candidates.append((symbol, info["base"]))

    print(f"通過 24 小時成交金額篩選的幣種數量:{len(candidates)} / {len(crypto_only)}")

    matches_by_interval = {}  # {interval: [(base, price, pct), ...]}

    # 只針對這次「該偵測」的週期,各自抓 K 線、各自用同一套條件判斷
    for interval in intervals:
        interval_ms = INTERVAL_MS.get(interval, 15 * 60 * 1000)
        matches = []
        for symbol, base_currency in candidates:
            try:
                klines = get_klines(session, symbol, interval, config["kline_fetch_limit"])
            except Exception as e:
                print(f"[警告] 取得 {symbol} {interval} K 線失敗:{e}")
                continue
            finally:
                time.sleep(config["request_sleep_sec"])

            matched, close_price, pct = evaluate_symbol(klines, config, interval_ms, now_ms)
            if matched:
                # 額外規則:1 小時級別要漲幅 >= min_pct_change_1h 才推播
                if interval == "60M" and pct < config.get("min_pct_change_1h", 0):
                    continue
                matches.append((base_currency, close_price, pct))

        matches_by_interval[interval] = matches
        print(f"[{interval}] 本次符合全部條件的幣種數量:{len(matches)}")

    total_matches = sum(len(m) for m in matches_by_interval.values())

    # 記錄這次已經處理過的1小時/4小時/日線K線邊界,避免下次重複觸發同一根
    if due_60m:
        state["last_60m_boundary_ms"] = latest_60m_time
    if due_4h:
        state["last_4h_boundary_ms"] = latest_4h_time
    if due_1d:
        state["last_1d_boundary_ms"] = latest_1d_time
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_run_intervals"] = intervals
    state["last_match_count"] = total_matches
    save_json(STATE_FILE, state)

    if total_matches == 0:
        print("本次偵測的週期都沒有符合條件的幣種,本次不發送通知。")
        return

    now_taipei_str = run_start_taipei.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"⚠️ Pionex 條件符合快訊 ({now_taipei_str} UTC+8)",
        "偵測條件",
        f"1.24小時成交量>{int(config['min_24h_amount_usdt'])}usdt",
        f"2.成交量>{config['vol_multiplier']}倍mavol{config['mavol_period']}",
        f"3.漲幅實體為前一根的{config['pct_change_multiplier']}倍",
        f"4.實體飽滿陽K(實體≥{int(config['min_body_ratio']*100)}%)",
        f"5.前一根上影線≤最新K線實體的{int(config['max_prev_wick_ratio']*100)}%",
    ]

    # 記錄每個幣種出現在哪些週期,供最後的「共振」區塊使用
    base_to_intervals = {}  # {base_currency_lower: [interval, ...]}

    for interval in intervals:
        matches = matches_by_interval.get(interval, [])
        if not matches:
            continue  # 這個週期沒有符合條件的幣種,整段不顯示
        label = INTERVAL_LABELS.get(interval, {}).get("full", interval)
        lines.append("=============================")
        lines.append(f"(當前偵測 {label})")
        for base_currency, close_price, pct in matches:
            base_lower = base_currency.lower()
            lines.append(
                f"{base_lower}: 上漲 {pct:.2f}%(現價{format_price(close_price)})"
            )
            base_to_intervals.setdefault(base_lower, []).append(interval)

    # 共振區塊:只有這次同時偵測了 2 個以上週期時才有意義(整點/4小時收線時)
    if len(intervals) >= 2:
        resonance = [
            (base, ivals) for base, ivals in base_to_intervals.items() if len(ivals) >= 2
        ]
        if resonance:
            lines.append("=============================")
            lines.append("共振")
            for base, ivals in resonance:
                short_labels = ",".join(
                    INTERVAL_LABELS.get(iv, {}).get("short", iv) for iv in ivals
                )
                lines.append(f"{base} ({short_labels})")

    message = "\n".join(lines)
    print(message)
    send_telegram_message(message)


if __name__ == "__main__":
    main()
