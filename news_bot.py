"""
news_bot.py - 키워드 뉴스봇
하보노 스타일: 그룹/키워드별 뉴스 수집 → 유사 뉴스 카운팅 → ★ 이상만 전송
GitHub Actions로 10분마다 실행
"""

import os
import json
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import feedparser

# ──────────────── 설정 ────────────────
TELEGRAM_TOKEN  = os.environ.get("NEWS_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("NEWS_CHAT_ID", "")

# 캐시 파일 (GitHub Actions에서는 artifact로 관리)
CACHE_FILE = Path("news_cache.json")

# 뉴스 중복 감지 윈도우 (2시간)
DEDUP_WINDOW_HOURS = 2

# 별점 기준 (2시간 내 유사 뉴스 등장 횟수)
STAR_THRESHOLDS = {
    1: 2,   # ★  : 2회 이상
    2: 4,   # ★★ : 4회 이상
    3: 7,   # ★★★: 7회 이상
}

# ──────────────── 그룹/키워드 ────────────────
KEYWORD_GROUPS = {
    "반도체": [
        "삼성전자", "SK하이닉스", "메모리", "엔비디아", "HBM",
        "마이크론", "TSMC", "GPU", "냉각", "패키징", "NVIDIA",
        "반도체", "HBM4", "HBM4E",
    ],
    "AI인프라": [
        "전력", "데이터센터", "AI 데이터센터", "냉각", "전력망",
        "HVDC", "전력반도체", "전력수요",
    ],
    "에너지": [
        "ESS", "배터리", "원전", "태양광", "SMR", "LNG",
        "풍력", "리튬", "우라늄", "블룸에너지",
    ],
    "로봇": [
        "피지컬AI", "휴머노이드", "자율주행", "보스턴다이내믹스",
        "로봇", "물리AI",
    ],
    "트럼프": [
        "트럼프", "관세", "상호관세", "백악관", "행정명령", "공화당",
    ],
    "금리/환율": [
        "기준금리", "금리인상", "연준", "PCE", "금리인하",
        "금리동결", "케빈 워시", "물가상승",
    ],
    "증권": [
        "목표주가", "상향", "하향", "리포트", "JP모건",
        "골드만삭스", "모건스탠리", "투자의견", "애널리스트",
    ],
    "국채금리": [
        "국채", "30년물", "채권시장", "금리상승", "재무부",
    ],
    "원자재": [
        "구리", "리튬", "알루미늄", "광산",
    ],
    "주식": [
        "수주", "배당", "M&A", "자사주", "신고가",
        "유상증자", "전환사채", "실적발표",
    ],
}

# ──────────────── 텔레그램 ────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정] {text[:80]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            }, timeout=10)
            if r.status_code == 200:
                return
        except Exception as e:
            print(f"텔레그램 전송 오류: {e}")
        time.sleep(2)

# ──────────────── 캐시 ────────────────
def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"news_seen": {}, "news_counts": {}}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def clean_old_cache(cache: dict) -> dict:
    """2시간 이상 된 캐시 삭제."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)).isoformat()
    cache["news_seen"] = {
        k: v for k, v in cache.get("news_seen", {}).items()
        if v.get("first_seen", "") >= cutoff
    }
    cache["news_counts"] = {
        k: v for k, v in cache.get("news_counts", {}).items()
        if k in cache["news_seen"]
    }
    return cache

# ──────────────── 뉴스 수집 ────────────────
def title_to_key(title: str) -> str:
    """제목 → 유사도 키 (앞 20자 기준 해시)."""
    clean = re.sub(r"[\[\]<>()「」『』\s]", "", title)
    return hashlib.md5(clean[:20].encode()).hexdigest()[:12]


def title_similarity_key(title: str) -> str:
    """핵심 단어만 추출해서 키 생성 (유사 뉴스 묶기)."""
    # 특수문자 제거, 조사 제거, 앞 15자
    clean = re.sub(r"[^\w]", "", title)
    return clean[:15]


def fetch_google_news(keyword: str, max_results: int = 20) -> list:
    """구글 뉴스 RSS로 키워드 뉴스 수집."""
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(keyword)}"
        f"&hl=ko&gl=KR&ceid=KR:ko"
    )
    try:
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:max_results]:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "").strip()
            pub   = entry.get("published", "")
            source = ""
            if hasattr(entry, "source"):
                source = entry.source.get("title", "")
            if title and link:
                results.append({
                    "title":  title,
                    "url":    link,
                    "source": source,
                    "pub":    pub,
                })
        return results
    except Exception as e:
        print(f"[RSS 오류] {keyword}: {e}")
        return []


def fetch_all_news() -> list:
    """모든 그룹/키워드 뉴스 수집."""
    all_news = []
    for group, keywords in KEYWORD_GROUPS.items():
        for keyword in keywords:
            items = fetch_google_news(keyword, max_results=10)
            for item in items:
                item["group"]   = group
                item["keyword"] = keyword
            all_news.extend(items)
            time.sleep(0.3)  # 요청 간격
    return all_news

# ──────────────── 별점 계산 ────────────────
def calc_stars(count: int) -> str:
    if count >= STAR_THRESHOLDS[3]:
        return "★★★"
    elif count >= STAR_THRESHOLDS[2]:
        return "★★"
    elif count >= STAR_THRESHOLDS[1]:
        return "★"
    return ""


def process_news(news_list: list, cache: dict) -> list:
    """뉴스 처리: 중복 카운팅 → 별점 계산 → 새 뉴스만 반환."""
    now = datetime.now(timezone.utc).isoformat()
    new_items = []

    for item in news_list:
        sim_key  = title_similarity_key(item["title"])
        url_key  = hashlib.md5(item["url"].encode()).hexdigest()[:12]

        # URL 기준 이미 전송한 뉴스는 skip
        if url_key in cache.get("news_seen", {}):
            # 카운트만 증가
            if sim_key in cache["news_counts"]:
                cache["news_counts"][sim_key] += 1
            continue

        # 유사 제목 카운트 업데이트
        if sim_key not in cache.get("news_counts", {}):
            cache.setdefault("news_counts", {})[sim_key] = 1
        else:
            cache["news_counts"][sim_key] += 1

        count = cache["news_counts"][sim_key]
        stars = calc_stars(count)

        # 별점 있는 것만 수집
        if stars:
            item["stars"] = stars
            item["count"] = count
            new_items.append(item)

        # 전송 여부와 무관하게 seen에 기록
        cache.setdefault("news_seen", {})[url_key] = {
            "title":      item["title"],
            "first_seen": now,
            "stars":      stars,
        }

    return new_items

# ──────────────── 시황 ────────────────
def fetch_market_summary() -> str:
    """장 시작 전 시황 요약 (yfinance)."""
    try:
        import yfinance as yf

        tickers = {
            "KOSPI":   "^KS11",
            "KOSDAQ":  "^KQ11",
            "S&P500":  "^GSPC",
            "나스닥":   "^IXIC",
            "다우":     "^DJI",
            "USD/KRW": "USDKRW=X",
        }

        lines = [
            f"📊 시황 요약 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"
        ]
        for name, ticker in tickers.items():
            try:
                t = yf.Ticker(ticker)
                info = t.fast_info
                price = info.last_price
                prev  = info.previous_close
                if price and prev:
                    chg     = price - prev
                    chg_pct = chg / prev * 100
                    sign    = "▲" if chg >= 0 else "▼"
                    if name == "USD/KRW":
                        lines.append(
                            f"💱 {name}: {price:,.2f}원 {sign}{abs(chg):.2f} ({chg_pct:+.2f}%)"
                        )
                    else:
                        lines.append(
                            f"  {name}: {price:,.2f} {sign}{abs(chg):.2f} ({chg_pct:+.2f}%)"
                        )
            except Exception:
                pass
        return "\n".join(lines)
    except ImportError:
        return "📊 시황 요약: yfinance 미설치"
    except Exception as e:
        return f"📊 시황 요약 오류: {e}"

# ──────────────── 메시지 포맷 ────────────────
def format_news_message(items: list) -> list:
    """뉴스 항목 → 텔레그램 메시지 리스트 (4096자 제한 분할)."""
    if not items:
        return []

    # 그룹별 묶기
    by_group = {}
    for item in items:
        g = item["group"]
        by_group.setdefault(g, []).append(item)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    messages = []
    current  = f"[뉴스 업데이트]\n업데이트: {now_str}\n"

    for group, group_items in by_group.items():
        # 별점 높은 순 정렬
        group_items.sort(key=lambda x: -len(x["stars"]))
        for item in group_items:
            block = (
                f"\n{item['stars']} 그룹: {group}\n"
                f"키워드: {item['keyword']}\n"
                f"제목: {item['title']}\n"
                f"발행처: {item['source']}\n"
                f"기사보기:\n{item['url']}\n"
            )
            if len(current) + len(block) > 4000:
                messages.append(current)
                current = f"[뉴스 업데이트 (계속)]\n{block}"
            else:
                current += block

    if current.strip():
        messages.append(current)
    return messages

# ──────────────── 메인 ────────────────
def run_news():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 뉴스봇 시작")

    cache = load_cache()
    cache = clean_old_cache(cache)

    # 시황 요약 (08:40~09:00 사이에만)
    hour = datetime.now().hour
    minute = datetime.now().minute
    if hour == 8 and 40 <= minute <= 59:
        summary = fetch_market_summary()
        if summary:
            send_telegram(summary)
            print("시황 요약 전송 완료")

    # 뉴스 수집
    print("뉴스 수집 중...")
    all_news = fetch_all_news()
    print(f"수집된 뉴스: {len(all_news)}건")

    # 처리 + 별점
    new_items = process_news(all_news, cache)
    print(f"★ 이상 뉴스: {len(new_items)}건")

    # 전송
    if new_items:
        messages = format_news_message(new_items)
        for msg in messages:
            send_telegram(msg)
            time.sleep(1)
        print(f"전송 완료: {len(messages)}개 메시지")
    else:
        print("전송할 뉴스 없음")

    # 캐시 저장
    save_cache(cache)
    print("완료")


if __name__ == "__main__":
    run_news()
