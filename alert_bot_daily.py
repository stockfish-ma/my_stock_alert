"""GitHub Actions용 헤드리스 알림봇.

용도: 일봉/주봉 신호만 검사. 한 번 실행 후 종료.
실행: python alert_bot_daily.py [kr|us|crypto]

PC 봇과의 차이:
- UI 없음 (Flet 의존성 없음)
- 텔레그램은 송신만 (양방향 명령 처리 안 함)
- 분봉/시간봉 안 씀 (장 시간 외에도 동작 가능)
- 환경변수에서 secrets 읽음 (GitHub Secrets용)
- watchlist.json만 읽고 알림 보내고 종료

워치리스트 형식은 PC 봇과 동일 (단, ma_unit은 day/week/month/price만).
"""

import os
import sys
import json
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ==================== 설정 ====================

# GitHub Secrets에서 읽음 (또는 로컬 테스트 시 환경변수)
KIS_APPKEY = os.environ.get("KIS_APPKEY", "")
KIS_SECRETKEY = os.environ.get("KIS_SECRETKEY", "")
KIS_VIRTUAL = os.environ.get("KIS_VIRTUAL", "true").lower() == "true"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 마켓 (한국/미국/코인)
MARKET_KR = "kr"
MARKET_US = "us"
MARKET_CRYPTO = "crypto"
MARKET_BADGES = {MARKET_KR: "🇰🇷", MARKET_US: "🇺🇸", MARKET_CRYPTO: "₿"}

# 봉 단위 (일봉 이상만)
UNIT_DAY = "day"
UNIT_WEEK = "week"
UNIT_MONTH = "month"
UNIT_LABELS = {UNIT_DAY: "일봉", UNIT_WEEK: "주봉", UNIT_MONTH: "월봉"}

# 도메인
KIS_DOMAIN = "https://openapivts.koreainvestment.com:29443" if KIS_VIRTUAL \
    else "https://openapi.koreainvestment.com:9443"
UPBIT_DOMAIN = "https://api.upbit.com/v1"


def log(msg):
    """타임스탬프 포함 로그 출력 (GitHub Actions 콘솔 출력)"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ==================== 텔레그램 ====================

def send_telegram(message, retries=2):
    """텔레그램 메시지 발송. 실패해도 봇은 종료 안 함."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ TELEGRAM_TOKEN/CHAT_ID 없음 - 알림 건너뜀")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                timeout=20,
            )
            if r.ok:
                return True
        except Exception:
            pass
        if attempt < retries:
            time.sleep(2)
    log(f"텔레그램 전송 실패")
    return False


# ==================== KIS API ====================

def get_kis_token():
    """KIS 토큰 발급 (매번 새로)."""
    if not KIS_APPKEY or not KIS_SECRETKEY:
        return None
    r = requests.post(
        f"{KIS_DOMAIN}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": KIS_APPKEY, "appsecret": KIS_SECRETKEY,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def kis_fetch_period(token, code, period="D", min_bars=70):
    """KIS 일/주/월봉 시세. 페이지네이션으로 min_bars 이상 받음."""
    if not token:
        return pd.DataFrame()

    all_rows = []
    end_date = datetime.now().strftime("%Y%m%d")
    start_date_obj = datetime.now() - timedelta(days=min_bars * 2)

    for page in range(5):  # 최대 5페이지 (= 약 500일)
        start_date = start_date_obj.strftime("%Y%m%d")
        url = f"{KIS_DOMAIN}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "authorization": f"Bearer {token}",
            "appkey": KIS_APPKEY,
            "appsecret": KIS_SECRETKEY,
            "tr_id": "FHKST03010100",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
        }
        for retry in range(3):
            try:
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.ok:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            break

        data = r.json()
        items = data.get("output2", [])
        if not items:
            break
        all_rows.extend(items)
        if len(all_rows) >= min_bars:
            break
        # 다음 페이지 (이전 기간)
        last_date = items[-1].get("stck_bsop_date")
        if not last_date:
            break
        end_date = (datetime.strptime(last_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        start_date_obj = datetime.strptime(end_date, "%Y%m%d") - timedelta(days=min_bars * 2)
        time.sleep(0.3)

    if not all_rows:
        return pd.DataFrame()

    rows = []
    for it in all_rows:
        try:
            rows.append({
                "date": pd.to_datetime(it["stck_bsop_date"]),
                "open": float(it["stck_oprc"]),
                "high": float(it["stck_hgpr"]),
                "low": float(it["stck_lwpr"]),
                "close": float(it["stck_clpr"]),
                "volume": float(it["acml_vol"]),
            })
        except (KeyError, ValueError):
            continue
    df = pd.DataFrame(rows).drop_duplicates(subset=["date"]).sort_values("date")
    df = df.set_index("date")
    return df


# ==================== yfinance (미국주식) ====================

def yf_fetch(ticker, period_unit="day", min_bars=70):
    """yfinance로 미국주식 봉 데이터."""
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance 미설치")
        return pd.DataFrame()

    interval_map = {UNIT_DAY: "1d", UNIT_WEEK: "1wk", UNIT_MONTH: "1mo"}
    period_map = {UNIT_DAY: "2y", UNIT_WEEK: "5y", UNIT_MONTH: "10y"}
    interval = interval_map.get(period_unit, "1d")
    period = period_map.get(period_unit, "2y")

    try:
        df = yf.download(ticker, period=period, interval=interval,
                            progress=False, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        # MultiIndex 컬럼이면 평탄화
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        log(f"yfinance fetch 에러 {ticker}: {e}")
        return pd.DataFrame()


# ==================== Upbit (코인) ====================

def upbit_fetch(market_code, period_unit="day"):
    """Upbit 일/주/월봉."""
    period_map = {UNIT_DAY: "days", UNIT_WEEK: "weeks", UNIT_MONTH: "months"}
    period = period_map.get(period_unit, "days")
    url = f"{UPBIT_DOMAIN}/candles/{period}"
    try:
        r = requests.get(url, params={"market": market_code, "count": 100}, timeout=10)
        if not r.ok:
            return pd.DataFrame()
        items = r.json()
        if not items:
            return pd.DataFrame()
        rows = []
        for it in items:
            rows.append({
                "date": pd.to_datetime(it["candle_date_time_kst"]),
                "open": float(it["opening_price"]),
                "high": float(it["high_price"]),
                "low": float(it["low_price"]),
                "close": float(it["trade_price"]),
                "volume": float(it["candle_acc_trade_volume"]),
            })
        df = pd.DataFrame(rows).sort_values("date").set_index("date")
        return df
    except Exception as e:
        log(f"Upbit fetch 에러 {market_code}: {e}")
        return pd.DataFrame()


# ==================== 신호 검출 ====================

def calc_macd(df, fast=12, slow=26, signal=9):
    """MACD 계산. (macd, signal, histogram) DataFrame 반환."""
    if df.empty or len(df) < slow + signal:
        return pd.DataFrame()
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return pd.DataFrame({"macd": macd, "signal": sig, "hist": hist}, index=df.index)


def check_breakout(df, ma_period):
    """이평선 돌파/이탈 검사."""
    if df.empty or len(df) < ma_period + 1:
        return None, None
    ma = df["close"].rolling(ma_period).mean()
    just = df.iloc[-1]
    prev = df.iloc[-2]
    just_ma = ma.iloc[-1]
    prev_ma = ma.iloc[-2]
    if pd.isna(just_ma) or pd.isna(prev_ma):
        return None, None
    if prev["close"] < prev_ma and just["close"] >= just_ma:
        return "🟢 상향 돌파", {"close": float(just["close"]), "ma": float(just_ma)}
    if prev["close"] > prev_ma and just["close"] <= just_ma:
        return "🔴 하향 이탈", {"close": float(just["close"]), "ma": float(just_ma)}
    return None, None


def check_cross(df, short_p, long_p):
    """골든/데드크로스 검사."""
    if df.empty or len(df) < long_p + 1:
        return None, None
    short_ma = df["close"].rolling(short_p).mean()
    long_ma = df["close"].rolling(long_p).mean()
    just_s, prev_s = short_ma.iloc[-1], short_ma.iloc[-2]
    just_l, prev_l = long_ma.iloc[-1], long_ma.iloc[-2]
    if any(pd.isna([just_s, prev_s, just_l, prev_l])):
        return None, None
    if prev_s <= prev_l and just_s > just_l:
        return "🟡 골든크로스", {"close": float(df["close"].iloc[-1]),
                                    "short_ma": float(just_s), "long_ma": float(just_l)}
    if prev_s >= prev_l and just_s < just_l:
        return "🔵 데드크로스", {"close": float(df["close"].iloc[-1]),
                                    "short_ma": float(just_s), "long_ma": float(just_l)}
    return None, None


def check_macd_cross(df, fast, slow, signal):
    """MACD 골든/데드크로스 검사."""
    macd_df = calc_macd(df, fast, slow, signal)
    if macd_df.empty or len(macd_df) < 2:
        return None, None
    just = macd_df.iloc[-1]
    prev = macd_df.iloc[-2]
    if any(pd.isna([just["macd"], just["signal"], prev["macd"], prev["signal"]])):
        return None, None
    bar_info = {
        "macd": float(just["macd"]),
        "signal": float(just["signal"]),
        "hist": float(just["hist"]),
        "close": float(df["close"].iloc[-1]),
    }
    if prev["macd"] <= prev["signal"] and just["macd"] > just["signal"]:
        return "🟡 MACD 골든크로스", bar_info
    if prev["macd"] >= prev["signal"] and just["macd"] < just["signal"]:
        return "🔵 MACD 데드크로스", bar_info
    return None, None


def check_bollinger(df, period=20, std_mult=2.0):
    """볼린저밴드 터치 검사."""
    if df.empty or len(df) < period + 1:
        return None, None
    middle = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = middle + std * std_mult
    lower = middle - std * std_mult
    just_high = df["high"].iloc[-1]
    just_low = df["low"].iloc[-1]
    just_close = df["close"].iloc[-1]
    just_upper = upper.iloc[-1]
    just_lower = lower.iloc[-1]
    just_middle = middle.iloc[-1]
    prev_high = df["high"].iloc[-2]
    prev_low = df["low"].iloc[-2]
    prev_upper = upper.iloc[-2]
    prev_lower = lower.iloc[-2]
    if any(pd.isna([just_upper, just_lower, prev_upper, prev_lower])):
        return None, None
    bar_info = {
        "close": float(just_close),
        "upper": float(just_upper), "lower": float(just_lower),
        "middle": float(just_middle),
    }
    if prev_high < prev_upper and just_high >= just_upper:
        return "🔴 BB 상단 터치", bar_info
    if prev_low > prev_lower and just_low <= just_lower:
        return "🔵 BB 하단 터치", bar_info
    return None, None


def check_price_target(df, target, direction):
    """가격 도달 알림."""
    if df.empty:
        return None, None
    just = df["close"].iloc[-1]
    prev = df["close"].iloc[-2] if len(df) > 1 else None
    if prev is None:
        return None, None
    if direction == "above" and prev < target and just >= target:
        return "💹 상향 도달", {"close": float(just), "target": float(target)}
    if direction == "below" and prev > target and just <= target:
        return "📉 하향 도달", {"close": float(just), "target": float(target)}
    return None, None


# ==================== 알림 메시지 포매팅 ====================

def format_price(market, price):
    if price is None:
        return "-"
    if market == MARKET_KR:
        return f"{int(price):,}원"
    if market == MARKET_CRYPTO:
        if price >= 1000:
            return f"{int(price):,}원"
        return f"{price:,.4f}원"
    if market == MARKET_US:
        return f"${price:,.2f}"
    return str(price)


def fetch_data(market, code, unit, token=None):
    """마켓별 데이터 fetch."""
    if market == MARKET_KR:
        period_map = {UNIT_DAY: "D", UNIT_WEEK: "W", UNIT_MONTH: "M"}
        return kis_fetch_period(token, code, period=period_map.get(unit, "D"))
    if market == MARKET_US:
        return yf_fetch(code, period_unit=unit)
    if market == MARKET_CRYPTO:
        return upbit_fetch(code, period_unit=unit)
    return pd.DataFrame()


def check_entry(entry, df, market, code, name):
    """워치리스트 엔트리 1개 검사."""
    badge = MARKET_BADGES.get(market, "")
    unit = entry.get("ma_unit", UNIT_DAY)
    unit_label = UNIT_LABELS.get(unit, unit)
    strategy = entry.get("strategy")

    # 가격 알림
    if unit == "price":
        target = entry.get("price_target")
        direction = entry.get("price_direction", "above")
        if target is None:
            return None
        sig, info = check_price_target(df, target, direction)
        if not sig:
            return None
        return (f"{sig} {badge}\n\n"
                f"종목: {name} ({code})\n"
                f"현재가: {format_price(market, info['close'])}\n"
                f"목표가: {format_price(market, info['target'])}")

    # MACD
    if strategy == "macd":
        f = entry.get("macd_fast", 12)
        s = entry.get("macd_slow", 26)
        sg = entry.get("macd_signal", 9)
        sig, info = check_macd_cross(df, f, s, sg)
        if not sig:
            return None
        return (f"{sig} {badge}\n\n"
                f"종목: {name} ({code})\n"
                f"봉단위: {unit_label}\n"
                f"MACD({f},{s},{sg})\n"
                f"종가: {format_price(market, info['close'])}\n"
                f"MACD: {info['macd']:.2f}\n"
                f"시그널: {info['signal']:.2f}\n"
                f"히스토그램: {info['hist']:.2f}")

    # BB
    if strategy == "bb":
        p = entry.get("bb_period", 20)
        std = entry.get("bb_std", 2.0)
        sig, info = check_bollinger(df, p, std)
        if not sig:
            return None
        return (f"{sig} {badge}\n\n"
                f"종목: {name} ({code})\n"
                f"봉단위: {unit_label}\n"
                f"BB({p}, {std})\n"
                f"종가: {format_price(market, info['close'])}\n"
                f"상단: {format_price(market, info['upper'])}\n"
                f"하단: {format_price(market, info['lower'])}")

    # 이평선 / 크로스
    short_p = entry.get("ma_period")
    long_p = entry.get("ma_long_period")
    if short_p is None:
        return None
    if long_p is None:
        # 단순 이평선
        sig, info = check_breakout(df, short_p)
        if not sig:
            return None
        return (f"{sig} {badge}\n\n"
                f"종목: {name} ({code})\n"
                f"봉단위: {unit_label}\n"
                f"이평선 {short_p}\n"
                f"종가: {format_price(market, info['close'])}\n"
                f"이평선: {format_price(market, info['ma'])}")
    # 크로스
    sig, info = check_cross(df, short_p, long_p)
    if not sig:
        return None
    return (f"{sig} {badge}\n\n"
            f"종목: {name} ({code})\n"
            f"봉단위: {unit_label}\n"
            f"{short_p}×{long_p} 크로스\n"
            f"종가: {format_price(market, info['close'])}\n"
            f"단기({short_p}): {format_price(market, info['short_ma'])}\n"
            f"장기({long_p}): {format_price(market, info['long_ma'])}")


# ==================== 메인 ====================

def load_watchlist():
    """watchlist.json 읽기. 마켓별로 필터링은 main에서."""
    p = Path("watchlist.json")
    if not p.exists():
        log("⚠️ watchlist.json 파일 없음")
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"watchlist.json 파싱 에러: {e}")
        return []


def main():
    """검사할 마켓 인자 받기. 'kr' 'us' 'crypto' 중 하나."""
    if len(sys.argv) < 2:
        log("사용법: python alert_bot_daily.py [kr|us|crypto]")
        sys.exit(1)
    target_market = sys.argv[1].lower()
    if target_market not in (MARKET_KR, MARKET_US, MARKET_CRYPTO):
        log(f"잘못된 마켓: {target_market}")
        sys.exit(1)

    log(f"=== 일봉 검사 시작 · 마켓: {target_market} ===")

    watchlist = load_watchlist()
    if not watchlist:
        log("워치리스트 비어있음")
        return

    # 마켓별 필터 + 일/주/월봉만
    entries = [e for e in watchlist
                  if e.get("market") == target_market
                  and e.get("ma_unit") in (UNIT_DAY, UNIT_WEEK, UNIT_MONTH, "price")]
    log(f"검사 대상: {len(entries)}개 신호")
    if not entries:
        return

    # KIS 토큰 (한국주식만 필요)
    token = None
    if target_market == MARKET_KR:
        try:
            token = get_kis_token()
            log("KIS 토큰 발급 OK")
        except Exception as e:
            log(f"KIS 토큰 발급 실패: {e}")
            return

    # 종목별로 그룹화 (같은 종목 같은 봉단위 데이터 한 번만 fetch)
    df_cache = {}  # (market, code, unit) → DataFrame
    alert_count = 0

    for entry in entries:
        market = entry["market"]
        code = entry["code"]
        name = entry.get("name", code)
        unit = entry.get("ma_unit", UNIT_DAY)

        # 가격 알림은 일봉 데이터로 검사
        fetch_unit = UNIT_DAY if unit == "price" else unit
        cache_key = (market, code, fetch_unit)
        if cache_key not in df_cache:
            df_cache[cache_key] = fetch_data(market, code, fetch_unit, token)
        df = df_cache[cache_key]

        if df.empty:
            log(f"⚠️ 데이터 없음: {name} ({code}) {UNIT_LABELS.get(fetch_unit, fetch_unit)}")
            continue

        try:
            msg = check_entry(entry, df, market, code, name)
        except Exception as e:
            log(f"검사 에러 {name}: {e}")
            continue

        if msg:
            send_telegram(msg)
            log(f"📤 알림: {name} {entry.get('strategy', '이평선')} {unit}")
            alert_count += 1
            time.sleep(0.5)  # 텔레그램 rate limit 방지

    log(f"=== 검사 완료 · 알림 {alert_count}개 발송 ===")


if __name__ == "__main__":
    main()
