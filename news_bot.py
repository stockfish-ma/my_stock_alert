"""
news_bot.py - 키워드 뉴스봇
- 텔레그램 명령어로 키워드 추가/삭제/조회
- 유사 뉴스 카운팅 → ★ 이상만 전송
- GitHub Actions 10분마다 실행
"""

import os, json, hashlib, re, time, base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import feedparser

# ──────────────── 설정 ────────────────
TELEGRAM_TOKEN   = os.environ.get("NEWS_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("NEWS_CHAT_ID", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPOSITORY", "")  # 자동 주입

CACHE_FILE    = Path("news_cache.json")
KEYWORDS_FILE = Path("keywords.json")

DEDUP_WINDOW_HOURS = 2
NEWS_MAX_AGE_HOURS = 6   # 최근 6시간 이내 기사만 처리

STAR_THRESHOLDS = {
    1: 3,   # ★  : 3회 이상
    2: 6,   # ★★ : 6회 이상
    3: 10,  # ★★★: 10회 이상
}

# ──────────────── 키워드 로드/저장 ────────────────
def load_keywords() -> dict:
    if KEYWORDS_FILE.exists():
        with open(KEYWORDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_keywords_local(kw: dict):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(kw, f, ensure_ascii=False, indent=2)


def _save_file_to_github(local_path: Path, commit_msg: str):
    """로컬 파일을 GitHub repo에 저장."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{local_path.name}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        content = base64.b64encode(local_path.read_bytes()).decode()
        r = requests.get(url, headers=headers, timeout=10)
        body = {"message": commit_msg, "content": content}
        if r.ok:
            body["sha"] = r.json().get("sha", "")
        requests.put(url, json=body, headers=headers, timeout=10)
    except Exception as e:
        print(f"GitHub 저장 오류 ({local_path.name}): {e}")


def save_keywords_github(kw: dict):
    """keywords.json GitHub 업데이트."""
    save_keywords_local(kw)
    _save_file_to_github(KEYWORDS_FILE, f"키워드 업데이트 ({datetime.now().strftime('%m-%d %H:%M')})")

# ──────────────── 텔레그램 ────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정]\n{text[:100]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            }, timeout=10)
            if r.ok:
                return
        except Exception as e:
            print(f"텔레그램 전송 오류: {e}")
        time.sleep(2)


def get_telegram_updates(offset: int = 0) -> list:
    """텔레그램 메시지 가져오기."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        if r.ok:
            return r.json().get("result", [])
    except Exception as e:
        print(f"getUpdates 오류: {e}")
    return []

# ──────────────── 명령어 처리 ────────────────
def handle_commands():
    """텔레그램 명령어 처리.

    /키워드            → 전체 키워드 목록 출력
    /추가 그룹명 키워드  → 키워드 추가
    /삭제 그룹명 키워드  → 키워드 삭제
    /그룹추가 그룹명    → 새 그룹 추가
    /그룹삭제 그룹명    → 그룹 전체 삭제
    """
    cache = load_cache()
    last_update_id = cache.get("last_update_id", 0)
    updates = get_telegram_updates(offset=last_update_id + 1)

    if not updates:
        return

    keywords = load_keywords()
    changed = False

    for update in updates:
        uid = update.get("update_id", 0)
        last_update_id = max(last_update_id, uid)

        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        # 등록된 채팅만 처리
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue
        if not text.startswith("/"):
            continue

        parts = text.split()
        cmd = parts[0].lower()

        # ── /키워드 ──────────────────────
        if cmd in ("/키워드", "/keywords"):
            lines = ["📋 현재 키워드 목록\n"]
            for group, kws in keywords.items():
                lines.append(f"[{group}]")
                lines.append("  " + ", ".join(kws))
            send_telegram("\n".join(lines))

        # ── /추가 그룹명 키워드 ──────────
        elif cmd in ("/추가", "/add") and len(parts) >= 3:
            group   = parts[1]
            keyword = " ".join(parts[2:])
            if group not in keywords:
                keywords[group] = []
            if keyword not in keywords[group]:
                keywords[group].append(keyword)
                changed = True
                send_telegram(f"✅ 추가됨: [{group}] {keyword}")
            else:
                send_telegram(f"이미 존재: [{group}] {keyword}")

        # ── /삭제 그룹명 키워드 ──────────
        elif cmd in ("/삭제", "/remove") and len(parts) >= 3:
            group   = parts[1]
            keyword = " ".join(parts[2:])
            if group in keywords and keyword in keywords[group]:
                keywords[group].remove(keyword)
                if not keywords[group]:
                    del keywords[group]
                changed = True
                send_telegram(f"🗑️ 삭제됨: [{group}] {keyword}")
            else:
                send_telegram(f"없는 키워드: [{group}] {keyword}")

        # ── /그룹추가 그룹명 ─────────────
        elif cmd in ("/그룹추가",) and len(parts) >= 2:
            group = parts[1]
            if group not in keywords:
                keywords[group] = []
                changed = True
                send_telegram(f"✅ 그룹 추가됨: {group}")
            else:
                send_telegram(f"이미 있는 그룹: {group}")

        # ── /그룹삭제 그룹명 ─────────────
        elif cmd in ("/그룹삭제",) and len(parts) >= 2:
            group = parts[1]
            if group in keywords:
                del keywords[group]
                changed = True
                send_telegram(f"🗑️ 그룹 삭제됨: {group}")
            else:
                send_telegram(f"없는 그룹: {group}")

        # ── /도움말 ──────────────────────
        elif cmd in ("/도움말", "/help"):
            send_telegram(
                "📌 뉴스봇 명령어\n\n"
                "/키워드 - 전체 키워드 목록\n"
                "/추가 [그룹] [키워드] - 키워드 추가\n"
                "/삭제 [그룹] [키워드] - 키워드 삭제\n"
                "/그룹추가 [그룹명] - 새 그룹 추가\n"
                "/그룹삭제 [그룹명] - 그룹 전체 삭제\n\n"
                "예시:\n"
                "/추가 반도체 AMD\n"
                "/삭제 트럼프 백악관"
            )

    # 변경사항 저장
    if changed:
        save_keywords_github(keywords)

    # update_id 캐시 저장
    cache["last_update_id"] = last_update_id
    save_cache(cache)

# ──────────────── 캐시 ────────────────
def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"news_seen": {}, "news_counts": {}, "last_update_id": 0}


def save_cache(cache: dict):
    """캐시 저장 - 로컬 + GitHub"""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    # GitHub에도 저장 (다음 실행 때도 유지)
    _save_file_to_github(CACHE_FILE, "news_cache.json 업데이트")


def clean_old_cache(cache: dict) -> dict:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=DEDUP_WINDOW_HOURS)
    ).isoformat()
    cache["news_seen"] = {
        k: v for k, v in cache.get("news_seen", {}).items()
        if v.get("first_seen", "") >= cutoff
    }
    # sim_stars도 seen에 없는 건 제거
    valid_keys = set()
    for item in cache["news_seen"].values():
        pass  # seen 기반으로 관리
    cache["news_counts"] = {
        k: v for k, v in cache.get("news_counts", {}).items()
    }
    # sim_stars는 DEDUP_WINDOW_HOURS 지나도 유지 (업그레이드 추적용)
    # 다만 너무 오래된 것은 정리 (24시간 초과)
    return cache

# ──────────────── 뉴스 수집 ────────────────
def title_similarity_key(title: str) -> str:
    clean = re.sub(r"[^\w]", "", title)
    return clean[:15]


def parse_pub_date(pub_str: str):
    """RSS 발행 시간 파싱 → UTC datetime."""
    if not pub_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub_str).astimezone(timezone.utc)
    except Exception:
        try:
            from datetime import datetime
            return datetime.strptime(
                pub_str[:25], "%a, %d %b %Y %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return None


def fetch_google_news(keyword: str, max_results: int = 10) -> list:
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(keyword)}"
        f"&hl=ko&gl=KR&ceid=KR:ko"
    )
    try:
        feed = feedparser.parse(url)
        results = []
        now_utc = datetime.now(timezone.utc)
        cutoff  = now_utc - timedelta(hours=NEWS_MAX_AGE_HOURS)

        for entry in feed.entries[:max_results]:
            title  = entry.get("title", "").strip()
            link   = entry.get("link", "").strip()
            pub    = entry.get("published", "")
            source = getattr(entry, "source", {})
            source = source.get("title", "") if isinstance(source, dict) else ""

            if not title or not link:
                continue

            # 발행 시간 필터
            pub_dt = parse_pub_date(pub)
            if pub_dt and pub_dt < cutoff:
                continue  # 너무 오래된 기사 skip

            results.append({
                "title":  title,
                "url":    link,
                "source": source,
                "pub_dt": pub_dt,
            })
        return results
    except Exception as e:
        print(f"[RSS 오류] {keyword}: {e}")
        return []


def fetch_all_news(keywords: dict) -> list:
    all_news = []
    for group, kw_list in keywords.items():
        for keyword in kw_list:
            items = fetch_google_news(keyword)
            for item in items:
                item["group"]   = group
                item["keyword"] = keyword
            all_news.extend(items)
            time.sleep(0.3)
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
    """뉴스 처리.

    - sim_key(제목 유사도) 기준 카운팅 → 다른 매체 같은 주제 = 카운트 증가
    - 별점 없음→★, ★→★★, ★★→★★★ 업그레이드 시 전송
    - URL 기준으로 중복 전송 방지 (같은 URL 두 번 전송 안 함)
    """
    now = datetime.now(timezone.utc).isoformat()
    new_items = []

    # sim_key별 대표 기사 (처음 본 것)
    sim_key_to_item = {}

    for item in news_list:
        sim_key = title_similarity_key(item["title"])
        url_key = hashlib.md5(item["url"].encode()).hexdigest()[:12]

        # sim_key 카운트 증가
        cache.setdefault("news_counts", {})[sim_key] = \
            cache["news_counts"].get(sim_key, 0) + 1
        count = cache["news_counts"][sim_key]
        stars = calc_stars(count)

        # 이전 별점 조회
        prev_stars = cache.get("sim_stars", {}).get(sim_key, "")

        # 별점 업그레이드 됐을 때만 전송
        if stars and stars != prev_stars and len(stars) > len(prev_stars):
            # 이 sim_key의 대표 URL로 전송 (이미 전송한 URL 제외)
            url_sent = cache.get("news_seen", {}).get(url_key)
            if not url_sent:
                item["stars"] = stars
                item["count"] = count
                new_items.append(item)
                cache.setdefault("news_seen", {})[url_key] = {
                    "first_seen": now,
                    "stars": stars,
                }
            # 별점 업데이트
            cache.setdefault("sim_stars", {})[sim_key] = stars

        # URL seen 기록 (전송 안 해도 기록)
        cache.setdefault("news_seen", {}).setdefault(url_key, {
            "first_seen": now,
            "stars": "",
        })

    return new_items

# ──────────────── 메시지 포맷 (간소화) ────────────────
def format_news_message(items: list) -> list:
    """키워드별로 묶어서 압축 전송."""
    if not items:
        return []

    # 별점 높은 순 정렬
    items.sort(key=lambda x: (-len(x["stars"]), x["group"]))

    # 키워드별 묶기
    from collections import defaultdict
    grouped = defaultdict(list)
    for item in items:
        key = (item["stars"], item["group"], item["keyword"])
        grouped[key].append(item)

    now_str = datetime.now().strftime("%m-%d %H:%M")
    messages = []
    current = f"📰 뉴스 {now_str}\n"

    for (stars, group, keyword), news_list in grouped.items():
        # 각 키워드 블록: 제목 최대 3개만
        titles = [n["title"] for n in news_list[:3]]
        urls   = [n["url"]   for n in news_list[:3]]

        block = f"\n{stars} [{group}] {keyword}\n"
        for i, (title, url, item) in enumerate(
            zip(titles, urls, news_list[:3])
        ):
            pub_str = ""
            if item.get("pub_dt"):
                kst = item["pub_dt"] + timedelta(hours=9)
                pub_str = f" ({kst.strftime('%m-%d %H:%M')})"
            block += f"· {title}{pub_str}\n  {url}\n"
        if len(news_list) > 3:
            block += f"  외 {len(news_list)-3}건 더\n"

        if len(current) + len(block) > 4000:
            messages.append(current)
            current = f"📰 뉴스 {now_str} (계속)\n{block}"
        else:
            current += block

    if current.strip():
        messages.append(current)
    return messages

# ──────────────── 시황 ────────────────
def fetch_market_summary() -> str:
    try:
        import yfinance as yf
        tickers = {
            "KOSPI":   "^KS11",
            "KOSDAQ":  "^KQ11",
            "S&P500":  "^GSPC",
            "나스닥":   "^IXIC",
            "USD/KRW": "USDKRW=X",
        }
        lines = [f"📊 시황 {datetime.now().strftime('%m-%d %H:%M')}\n"]
        for name, ticker in tickers.items():
            try:
                info = yf.Ticker(ticker).fast_info
                price, prev = info.last_price, info.previous_close
                if price and prev:
                    chg = price - prev
                    pct = chg / prev * 100
                    sign = "▲" if chg >= 0 else "▼"
                    if name == "USD/KRW":
                        lines.append(f"💱 {name} {price:,.1f} {sign}{abs(pct):.2f}%")
                    else:
                        lines.append(f"  {name} {price:,.2f} {sign}{abs(pct):.2f}%")
            except Exception:
                pass
        return "\n".join(lines)
    except Exception as e:
        return f"📊 시황 오류: {e}"

# ──────────────── 메인 ────────────────
def run_news():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 뉴스봇 시작")

    # 1. 명령어 처리 (키워드 추가/삭제/조회)
    handle_commands()

    # 2. 캐시 로드 + 정리
    cache = load_cache()
    cache = clean_old_cache(cache)

    # 3. 시황 (매일 07:00~07:20)
    hour, minute = datetime.now().hour, datetime.now().minute
    if hour == 7 and minute <= 20:
        summary = fetch_market_summary()
        if summary:
            send_telegram(summary)
            print("시황 전송 완료")

    # 4. 뉴스 수집
    keywords = load_keywords()
    if not keywords:
        print("키워드 없음 - 종료")
        return

    print(f"키워드 {sum(len(v) for v in keywords.values())}개 수집 중...")
    all_news = fetch_all_news(keywords)
    print(f"수집: {len(all_news)}건")

    # 5. 별점 계산
    new_items = process_news(all_news, cache)
    print(f"★ 이상: {len(new_items)}건")

    # 6. 전송
    if new_items:
        messages = format_news_message(new_items)
        for msg in messages:
            send_telegram(msg)
            time.sleep(1)
        print(f"전송: {len(messages)}개 메시지")
    else:
        print("전송할 뉴스 없음")

    save_cache(cache)
    print("완료")


if __name__ == "__main__":
    run_news()
