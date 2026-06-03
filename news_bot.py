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
GITHUB_REPO      = os.environ.get("GITHUB_REPOSITORY", "")
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

CACHE_FILE    = Path("news_cache.json")
KEYWORDS_FILE = Path("keywords.json")

DEDUP_WINDOW_HOURS = 12  # 12시간 내 반복 횟수로 중요도 판단
NEWS_MAX_AGE_HOURS = 6   # 최근 6시간 이내 기사만 처리

# 전송 기준 (12시간 내 등장 횟수)
SEND_THRESHOLD  = 3   # 3회 이상이면 전송
UPGRADE_COUNTS  = [5, 12, 25]  # 5회, 12회, 25회 달성 시 전송

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
def send_telegram(text: str, parse_mode: str = "HTML"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정]\n{text[:100]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            if r.ok:
                return
            print(f"텔레그램 오류: {r.text}")
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
    cache = load_cache()
    last_update_id = cache.get("last_update_id", 0)
    updates = get_telegram_updates(offset=last_update_id + 1)

    print(f"[명령어] 업데이트 {len(updates)}건 (last_id={last_update_id})")
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

        print(f"[명령어] chat_id={chat_id} expected={TELEGRAM_CHAT_ID} text={text[:30]}")

        # 등록된 채팅만 처리
        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"[명령어] chat_id 불일치 skip")
            continue
        if not text.startswith("/"):
            continue

        parts = text.split()
        # @봇이름 제거 (/키워드@Farmmerr_bot → /키워드)
        cmd = parts[0].lower().split("@")[0]

        # ── /키워드 ──────────────────────
        # ── /키워드 ──────────────────────
        if cmd in ("/키워드", "/keywords"):
            lines = ["📋 현재 키워드 목록"]
            for group, kws in keywords.items():
                lines.append("")
                lines.append(f"[{group}]")
                lines.append("  " + ", ".join(kws))
            send_telegram("\n".join(lines), parse_mode="")

        # ── /추가 그룹명 키워드 ──────────
        elif cmd in ("/추가", "/add") and len(parts) >= 3:
            group   = parts[1]
            keyword = " ".join(parts[2:])
            if group not in keywords:
                keywords[group] = []
            if keyword not in keywords[group]:
                keywords[group].append(keyword)
                changed = True
                send_telegram(f"✅ 추가됨: [{group}] {keyword}", parse_mode="")
            else:
                send_telegram(f"이미 존재: [{group}] {keyword}", parse_mode="")

        # ── /삭제 그룹명 키워드 ──────────
        elif cmd in ("/삭제", "/remove") and len(parts) >= 3:
            group   = parts[1]
            keyword = " ".join(parts[2:])
            if group in keywords and keyword in keywords[group]:
                keywords[group].remove(keyword)
                if not keywords[group]:
                    del keywords[group]
                changed = True
                send_telegram(f"🗑️ 삭제됨: [{group}] {keyword}", parse_mode="")
            else:
                send_telegram(f"없는 키워드: [{group}] {keyword}", parse_mode="")

        # ── /그룹추가 그룹명 ─────────────
        elif cmd in ("/그룹추가",) and len(parts) >= 2:
            group = parts[1]
            if group not in keywords:
                keywords[group] = []
                changed = True
                send_telegram(f"✅ 그룹 추가됨: {group}", parse_mode="")
            else:
                send_telegram(f"이미 있는 그룹: {group}", parse_mode="")

        # ── /그룹삭제 그룹명 ─────────────
        elif cmd in ("/그룹삭제",) and len(parts) >= 2:
            group = parts[1]
            if group in keywords:
                del keywords[group]
                changed = True
                send_telegram(f"🗑️ 그룹 삭제됨: {group}", parse_mode="")
            else:
                send_telegram(f"없는 그룹: {group}", parse_mode="")

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
    # 오래된 URL seen 정리
    cache["news_seen"] = {
        k: v for k, v in cache.get("news_seen", {}).items()
        if v.get("first_seen", "") >= cutoff
    }
    # 12시간마다 키워드 카운트 리셋 (새 사이클)
    last_reset = cache.get("kw_counts_reset", "")
    if not last_reset or last_reset < cutoff:
        cache["kw_counts"] = {}
        cache["kw_counts_reset"] = datetime.now(timezone.utc).isoformat()
    return cache

# ──────────────── 뉴스 수집 ────────────────
def clean_title(title: str) -> str:
    """제목에서 발행처 중복 제거.
    예: '삼성전자 HBM4E - 연합뉴스 - 연합뉴스' → '삼성전자 HBM4E - 연합뉴스'
    """
    # ' - 발행처 - 발행처' 패턴 제거
    parts = title.split(' - ')
    if len(parts) >= 3 and parts[-1].strip() == parts[-2].strip():
        parts = parts[:-1]
    return ' - '.join(parts).strip()


def clean_source(source: str) -> str:
    """발행처 정리 - v.daum.net 등 대체."""
    noise_sources = {
        'v.daum.net': '다음',
        'newsen.com': '뉴스엔',
        'ekn.kr': '에너지경제',
        '2news.co.kr': '투데이뉴스',
        'koreasprint.com': '코리아스프린트',
        'vietnam.vn': 'Vietnam.vn',
        'investing.com 한국어': 'Investing.com',
    }
    s = source.lower()
    for k, v in noise_sources.items():
        if k in s:
            return v
    return source


# 노이즈 키워드 필터 (이 단어가 제목에 있으면 skip)
NOISE_TITLE_KEYWORDS = [
    '광산구', '광산을', '광산의', '광산김씨',
    '선거후보', '버스킹', '족보',
    'TXT', '포토엔', '[포토]',
    '구리시', '구리 시장', '구리시장',
    '[부고]', '부고', '부친상', '모친상', '별세',
]


def title_similarity_key(title: str) -> str:
    clean = re.sub(r"[^\w]", "", title)
    return clean[:15]


# 제외할 발행처 도메인
NOISE_SOURCES = {
    'v.daum.net', 'newsen.com', 'naver.com',
}


def is_noise_source(source: str) -> bool:
    s = source.lower()
    return any(ns in s for ns in NOISE_SOURCES)


def is_noise(title: str) -> bool:
    """노이즈 뉴스 필터."""
    for kw in NOISE_TITLE_KEYWORDS:
        if kw in title:
            return True
    return False
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


def parse_pub_date(pub_str: str):
    """RSS/네이버 발행 시간 파싱 → UTC datetime."""
    if not pub_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub_str).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.strptime(
                pub_str[:19], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return None


def is_korean_keyword(keyword: str) -> bool:
    """한글 포함 여부로 한국어 키워드 판단."""
    return any('\uAC00' <= c <= '\uD7A3' for c in keyword)


def fetch_naver_news(keyword: str, max_results: int = 10) -> list:
    """네이버 뉴스 검색 API - 한국어 키워드 전용."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query":  keyword,
        "display": max_results,
        "sort":   "date",  # 최신순
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if not r.ok:
            print(f"[네이버 오류] {keyword}: {r.status_code}")
            return []

        items = r.json().get("items", [])
        now_utc = datetime.now(timezone.utc)
        cutoff  = now_utc - timedelta(hours=NEWS_MAX_AGE_HOURS)
        results = []

        for item in items:
            # HTML 태그 제거
            title  = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
            title  = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            link   = item.get("originallink") or item.get("link", "")
            source = item.get("source", "")
            pub    = item.get("pubDate", "")

            if not title or not link:
                continue
            if is_noise(title):
                continue
            if keyword.lower() not in title.lower():
                continue

            # 발행 시간 필터
            pub_dt = parse_pub_date(pub)
            if pub_dt and pub_dt < cutoff:
                continue

            results.append({
                "title":  title,
                "url":    link,
                "source": source,
                "pub_dt": pub_dt,
            })
        return results
    except Exception as e:
        print(f"[네이버 오류] {keyword}: {e}")
        return []


def fetch_google_news(keyword: str, max_results: int = 10) -> list:
    # 따옴표로 감싸서 정확한 검색 (연관 검색 방지)
    exact_kw = f'"{keyword}"' if len(keyword) <= 6 else keyword
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(exact_kw)}"
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

            # 노이즈 필터
            if is_noise(title):
                continue

            # 노이즈 소스 필터
            if is_noise_source(source):
                continue

            # 제목에 키워드 포함 여부 검증 (구글 연관검색 방지)
            # keyword가 2글자 이상이면 제목에 있어야 함
            if len(keyword) >= 2 and keyword.lower() not in title.lower():
                continue

            # 제목/발행처 정리
            title  = clean_title(title)
            source = clean_source(source)

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
    """키워드별 뉴스 수집.
    한국어 키워드 → 네이버 API (정확)
    영문 키워드   → 구글 RSS (해외 뉴스)
    """
    all_news = []
    for group, kw_list in keywords.items():
        for keyword in kw_list:
            if is_korean_keyword(keyword) and NAVER_CLIENT_ID:
                items = fetch_naver_news(keyword)
            else:
                items = fetch_google_news(keyword)
            for item in items:
                item["group"]   = group
                item["keyword"] = keyword
            all_news.extend(items)
            time.sleep(0.3)
    return all_news

# ──────────────── 키워드 기준 카운팅 ────────────────
def should_send(count: int, prev_count: int) -> bool:
    """전송 기준 도달 여부."""
    for threshold in UPGRADE_COUNTS:
        if prev_count < threshold <= count:
            return True
    return False


def count_label(count: int) -> str:
    return f"[{count}회]"


def process_news(news_list: list, cache: dict) -> None:
    """뉴스 수집 + 카운팅만. 전송은 하지 않음.
    1시간마다 TOP 10 전송은 send_top10()에서 처리.
    """
    now = datetime.now(timezone.utc).isoformat()
    from collections import defaultdict
    keyword_new_articles = defaultdict(list)

    for item in news_list:
        url_key = hashlib.md5(item["url"].encode()).hexdigest()[:12]
        if url_key in cache.get("news_seen", {}):
            continue
        cache.setdefault("news_seen", {})[url_key] = {"first_seen": now}
        kw_key = f"{item['group']}:{item['keyword']}"
        keyword_new_articles[kw_key].append(item)

    # 키워드별 카운트 + 대표 기사 저장
    for kw_key, articles in keyword_new_articles.items():
        new_count = len(articles)
        prev_count = cache.get("kw_counts", {}).get(kw_key, 0)
        total_count = prev_count + new_count
        cache.setdefault("kw_counts", {})[kw_key] = total_count

        # 대표 기사: 1시간 누적 기사 중 가장 많이 나온 제목
        # 기존 누적 기사 목록 + 새 기사 합치기
        prev_articles = cache.get("kw_article_pool", {}).get(kw_key, [])
        all_articles  = prev_articles + [
            {
                "title":   a["title"],
                "url":     a["url"],
                "source":  a.get("source", ""),
                "group":   a.get("group", ""),
                "keyword": a.get("keyword", ""),
            }
            for a in articles
        ]
        # 중복 URL 제거
        seen_urls = set()
        deduped = []
        for art in all_articles:
            if art["url"] not in seen_urls:
                seen_urls.add(art["url"])
                deduped.append(art)
        cache.setdefault("kw_article_pool", {})[kw_key] = deduped[-50:]

        # 가장 많이 나온 제목 찾기
        from collections import Counter
        title_counts = Counter(a["title"] for a in all_articles)
        best_title = title_counts.most_common(1)[0][0]
        best_article = next(a for a in all_articles if a["title"] == best_title)

        cache.setdefault("kw_articles", {})[kw_key] = {
            "title":   best_article["title"],
            "url":     best_article["url"],
            "source":  best_article.get("source", ""),
            "count":   total_count,
            "group":   articles[0]["group"],
            "keyword": articles[0]["keyword"],
        }


def get_all_news(cache: dict) -> list:
    """3건 이상 수집된 키워드만 반환."""
    article_pool = cache.get("kw_article_pool", {})
    kw_counts    = cache.get("kw_counts", {})
    if not article_pool:
        return []

    result = []
    for kw_key, articles in article_pool.items():
        if not articles:
            continue
        count = kw_counts.get(kw_key, len(articles))
        if count < 3:  # 3건 미만 제외
            continue
        group   = articles[0].get("group", "")
        keyword = articles[0].get("keyword", "")
        result.append({
            "group":    group,
            "keyword":  keyword,
            "count":    count,
            "articles": articles,
        })

    result.sort(key=lambda x: -x["count"])
    return result


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
        kst_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%m-%d %H:%M')
        lines = [f"📊 시황 {kst_str} KST\n"]
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


def format_news_message(groups: list) -> list:
    """키워드별 전체 기사 목록 포맷. 4096자 초과 시 분할."""
    if not groups:
        return []

    now_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M (KST)")
    messages = []
    current  = f"📰 뉴스 ({now_str})\n"

    for grp in groups:
        group   = grp["group"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        kw      = grp["keyword"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        count   = grp["count"]
        articles = grp["articles"]

        block = f"\n[{group}] {kw} {count}건\n"
        for art in articles:
            title = art.get("title", "")
            # 발행처 중복 제거
            parts = title.split(" - ")
            if len(parts) >= 3 and parts[-1].strip() == parts[-2].strip():
                parts = parts[:-1]
            if len(parts) >= 2:
                title = " - ".join(parts[:-1]).strip()
            else:
                title = parts[0].strip()

            source = clean_source(art.get("source", ""))
            url    = art.get("url", "")
            title  = title.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            src_str = f" - {source}" if source else ""
            block += f'  └ <a href="{url}">{title}{src_str}</a>\n'

        if len(current) + len(block) > 4000:
            messages.append(current)
            current = f"📰 뉴스 ({now_str}) (계속)\n{block}"
        else:
            current += block

    if current.strip():
        messages.append(current)
    return messages


def should_send_top10(cache: dict) -> bool:
    """1시간마다 TOP 10 전송 여부."""
    last = cache.get("last_top10_sent", "")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    now_dt  = datetime.now(timezone.utc)
    return (now_dt - last_dt).total_seconds() >= 3600


# ──────────────── 메인 ────────────────
def run_news():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 뉴스봇 시작")

    # KST 22:00~07:00 사이엔 실행 안 함
    kst_now  = datetime.now(timezone.utc) + timedelta(hours=9)
    kst_hour = kst_now.hour
    if kst_hour >= 22 or kst_hour < 7:
        print(f"[종료] KST {kst_hour}시 - 운영 시간 외 (07:00~22:00)")
        return

    # 1. 명령어 처리
    handle_commands()

    # 2. 캐시 로드 + 정리
    cache = load_cache()
    cache = clean_old_cache(cache)

    # 3. 시황 (매일 07:00~07:20 KST)
    if kst_hour == 7 and kst_now.minute <= 20:
        summary = fetch_market_summary()
        if summary:
            send_telegram(summary, parse_mode="")
            print("시황 전송 완료")

    # 4. 뉴스 수집 + 카운팅 (매 1분, 전송 안 함)
    keywords = load_keywords()
    if not keywords:
        print("키워드 없음 - 종료")
        return

    print(f"키워드 {sum(len(v) for v in keywords.values())}개 수집 중...")
    all_news = fetch_all_news(keywords)
    print(f"수집: {len(all_news)}건")

    process_news(all_news, cache)
    print("카운팅 완료")

    # 5. 1시간마다 수집된 뉴스 전송
    if should_send_top10(cache):
        groups = get_all_news(cache)
        if groups:
            messages = format_news_message(groups)
            for msg in messages:
                send_telegram(msg)
                time.sleep(1)
            print(f"뉴스 {len(groups)}개 그룹 전송 완료")
        else:
            print("수집된 뉴스 없음 - 전송 안 함")
        # 리셋
        cache["kw_counts"]       = {}
        cache["kw_articles"]     = {}
        cache["kw_article_pool"] = {}
        cache["last_top10_sent"] = datetime.now(timezone.utc).isoformat()

    save_cache(cache)
    print("완료")


if __name__ == "__main__":
    run_news()"""
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
GITHUB_REPO      = os.environ.get("GITHUB_REPOSITORY", "")
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

CACHE_FILE    = Path("news_cache.json")
KEYWORDS_FILE = Path("keywords.json")

DEDUP_WINDOW_HOURS = 12  # 12시간 내 반복 횟수로 중요도 판단
NEWS_MAX_AGE_HOURS = 6   # 최근 6시간 이내 기사만 처리

# 전송 기준 (12시간 내 등장 횟수)
SEND_THRESHOLD  = 3   # 3회 이상이면 전송
UPGRADE_COUNTS  = [5, 12, 25]  # 5회, 12회, 25회 달성 시 전송

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
def send_telegram(text: str, parse_mode: str = "HTML"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[텔레그램 미설정]\n{text[:100]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            if r.ok:
                return
            print(f"텔레그램 오류: {r.text}")
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
    cache = load_cache()
    last_update_id = cache.get("last_update_id", 0)
    updates = get_telegram_updates(offset=last_update_id + 1)

    print(f"[명령어] 업데이트 {len(updates)}건 (last_id={last_update_id})")
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

        print(f"[명령어] chat_id={chat_id} expected={TELEGRAM_CHAT_ID} text={text[:30]}")

        # 등록된 채팅만 처리
        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"[명령어] chat_id 불일치 skip")
            continue
        if not text.startswith("/"):
            continue

        parts = text.split()
        # @봇이름 제거 (/키워드@Farmmerr_bot → /키워드)
        cmd = parts[0].lower().split("@")[0]

        # ── /키워드 ──────────────────────
        # ── /키워드 ──────────────────────
        if cmd in ("/키워드", "/keywords"):
            lines = ["📋 현재 키워드 목록"]
            for group, kws in keywords.items():
                lines.append("")
                lines.append(f"[{group}]")
                lines.append("  " + ", ".join(kws))
            send_telegram("\n".join(lines), parse_mode="")

        # ── /추가 그룹명 키워드 ──────────
        elif cmd in ("/추가", "/add") and len(parts) >= 3:
            group   = parts[1]
            keyword = " ".join(parts[2:])
            if group not in keywords:
                keywords[group] = []
            if keyword not in keywords[group]:
                keywords[group].append(keyword)
                changed = True
                send_telegram(f"✅ 추가됨: [{group}] {keyword}", parse_mode="")
            else:
                send_telegram(f"이미 존재: [{group}] {keyword}", parse_mode="")

        # ── /삭제 그룹명 키워드 ──────────
        elif cmd in ("/삭제", "/remove") and len(parts) >= 3:
            group   = parts[1]
            keyword = " ".join(parts[2:])
            if group in keywords and keyword in keywords[group]:
                keywords[group].remove(keyword)
                if not keywords[group]:
                    del keywords[group]
                changed = True
                send_telegram(f"🗑️ 삭제됨: [{group}] {keyword}", parse_mode="")
            else:
                send_telegram(f"없는 키워드: [{group}] {keyword}", parse_mode="")

        # ── /그룹추가 그룹명 ─────────────
        elif cmd in ("/그룹추가",) and len(parts) >= 2:
            group = parts[1]
            if group not in keywords:
                keywords[group] = []
                changed = True
                send_telegram(f"✅ 그룹 추가됨: {group}", parse_mode="")
            else:
                send_telegram(f"이미 있는 그룹: {group}", parse_mode="")

        # ── /그룹삭제 그룹명 ─────────────
        elif cmd in ("/그룹삭제",) and len(parts) >= 2:
            group = parts[1]
            if group in keywords:
                del keywords[group]
                changed = True
                send_telegram(f"🗑️ 그룹 삭제됨: {group}", parse_mode="")
            else:
                send_telegram(f"없는 그룹: {group}", parse_mode="")

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
    # 오래된 URL seen 정리
    cache["news_seen"] = {
        k: v for k, v in cache.get("news_seen", {}).items()
        if v.get("first_seen", "") >= cutoff
    }
    # 12시간마다 키워드 카운트 리셋 (새 사이클)
    last_reset = cache.get("kw_counts_reset", "")
    if not last_reset or last_reset < cutoff:
        cache["kw_counts"] = {}
        cache["kw_counts_reset"] = datetime.now(timezone.utc).isoformat()
    return cache

# ──────────────── 뉴스 수집 ────────────────
def clean_title(title: str) -> str:
    """제목에서 발행처 중복 제거.
    예: '삼성전자 HBM4E - 연합뉴스 - 연합뉴스' → '삼성전자 HBM4E - 연합뉴스'
    """
    # ' - 발행처 - 발행처' 패턴 제거
    parts = title.split(' - ')
    if len(parts) >= 3 and parts[-1].strip() == parts[-2].strip():
        parts = parts[:-1]
    return ' - '.join(parts).strip()


def clean_source(source: str) -> str:
    """발행처 정리 - v.daum.net 등 대체."""
    noise_sources = {
        'v.daum.net': '다음',
        'newsen.com': '뉴스엔',
        'ekn.kr': '에너지경제',
        '2news.co.kr': '투데이뉴스',
        'koreasprint.com': '코리아스프린트',
        'vietnam.vn': 'Vietnam.vn',
        'investing.com 한국어': 'Investing.com',
    }
    s = source.lower()
    for k, v in noise_sources.items():
        if k in s:
            return v
    return source


# 노이즈 키워드 필터 (이 단어가 제목에 있으면 skip)
NOISE_TITLE_KEYWORDS = [
    '광산구', '광산을', '광산의', '광산김씨',
    '선거후보', '버스킹', '족보',
    'TXT', '포토엔', '[포토]',
    '구리시', '구리 시장', '구리시장',
    '[부고]', '부고', '부친상', '모친상', '별세',
]


def title_similarity_key(title: str) -> str:
    clean = re.sub(r"[^\w]", "", title)
    return clean[:15]


# 제외할 발행처 도메인
NOISE_SOURCES = {
    'v.daum.net', 'newsen.com', 'naver.com',
}


def is_noise_source(source: str) -> bool:
    s = source.lower()
    return any(ns in s for ns in NOISE_SOURCES)


def is_noise(title: str) -> bool:
    """노이즈 뉴스 필터."""
    for kw in NOISE_TITLE_KEYWORDS:
        if kw in title:
            return True
    return False
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


def parse_pub_date(pub_str: str):
    """RSS/네이버 발행 시간 파싱 → UTC datetime."""
    if not pub_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub_str).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.strptime(
                pub_str[:19], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return None


def is_korean_keyword(keyword: str) -> bool:
    """한글 포함 여부로 한국어 키워드 판단."""
    return any('\uAC00' <= c <= '\uD7A3' for c in keyword)


def fetch_naver_news(keyword: str, max_results: int = 10) -> list:
    """네이버 뉴스 검색 API - 한국어 키워드 전용."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query":  keyword,
        "display": max_results,
        "sort":   "date",  # 최신순
    }
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if not r.ok:
            print(f"[네이버 오류] {keyword}: {r.status_code}")
            return []

        items = r.json().get("items", [])
        now_utc = datetime.now(timezone.utc)
        cutoff  = now_utc - timedelta(hours=NEWS_MAX_AGE_HOURS)
        results = []

        for item in items:
            # HTML 태그 제거
            title  = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
            title  = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            link   = item.get("originallink") or item.get("link", "")
            source = item.get("source", "")
            pub    = item.get("pubDate", "")

            if not title or not link:
                continue
            if is_noise(title):
                continue
            if keyword.lower() not in title.lower():
                continue

            # 발행 시간 필터
            pub_dt = parse_pub_date(pub)
            if pub_dt and pub_dt < cutoff:
                continue

            results.append({
                "title":  title,
                "url":    link,
                "source": source,
                "pub_dt": pub_dt,
            })
        return results
    except Exception as e:
        print(f"[네이버 오류] {keyword}: {e}")
        return []


def fetch_google_news(keyword: str, max_results: int = 10) -> list:
    # 따옴표로 감싸서 정확한 검색 (연관 검색 방지)
    exact_kw = f'"{keyword}"' if len(keyword) <= 6 else keyword
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(exact_kw)}"
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

            # 노이즈 필터
            if is_noise(title):
                continue

            # 노이즈 소스 필터
            if is_noise_source(source):
                continue

            # 제목에 키워드 포함 여부 검증 (구글 연관검색 방지)
            # keyword가 2글자 이상이면 제목에 있어야 함
            if len(keyword) >= 2 and keyword.lower() not in title.lower():
                continue

            # 제목/발행처 정리
            title  = clean_title(title)
            source = clean_source(source)

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
    """키워드별 뉴스 수집.
    한국어 키워드 → 네이버 API (정확)
    영문 키워드   → 구글 RSS (해외 뉴스)
    """
    all_news = []
    for group, kw_list in keywords.items():
        for keyword in kw_list:
            if is_korean_keyword(keyword) and NAVER_CLIENT_ID:
                items = fetch_naver_news(keyword)
            else:
                items = fetch_google_news(keyword)
            for item in items:
                item["group"]   = group
                item["keyword"] = keyword
            all_news.extend(items)
            time.sleep(0.3)
    return all_news

# ──────────────── 키워드 기준 카운팅 ────────────────
def should_send(count: int, prev_count: int) -> bool:
    """전송 기준 도달 여부."""
    for threshold in UPGRADE_COUNTS:
        if prev_count < threshold <= count:
            return True
    return False


def count_label(count: int) -> str:
    return f"[{count}회]"


def process_news(news_list: list, cache: dict) -> None:
    """뉴스 수집 + 카운팅만. 전송은 하지 않음.
    1시간마다 TOP 10 전송은 send_top10()에서 처리.
    """
    now = datetime.now(timezone.utc).isoformat()
    from collections import defaultdict
    keyword_new_articles = defaultdict(list)

    for item in news_list:
        url_key = hashlib.md5(item["url"].encode()).hexdigest()[:12]
        if url_key in cache.get("news_seen", {}):
            continue
        cache.setdefault("news_seen", {})[url_key] = {"first_seen": now}
        kw_key = f"{item['group']}:{item['keyword']}"
        keyword_new_articles[kw_key].append(item)

    # 키워드별 카운트 + 대표 기사 저장
    for kw_key, articles in keyword_new_articles.items():
        new_count = len(articles)
        prev_count = cache.get("kw_counts", {}).get(kw_key, 0)
        total_count = prev_count + new_count
        cache.setdefault("kw_counts", {})[kw_key] = total_count

        # 대표 기사: 1시간 누적 기사 중 가장 많이 나온 제목
        # 기존 누적 기사 목록 + 새 기사 합치기
        prev_articles = cache.get("kw_article_pool", {}).get(kw_key, [])
        all_articles  = prev_articles + [
            {
                "title":   a["title"],
                "url":     a["url"],
                "source":  a.get("source", ""),
                "group":   a.get("group", ""),
                "keyword": a.get("keyword", ""),
            }
            for a in articles
        ]
        # 중복 URL 제거
        seen_urls = set()
        deduped = []
        for art in all_articles:
            if art["url"] not in seen_urls:
                seen_urls.add(art["url"])
                deduped.append(art)
        cache.setdefault("kw_article_pool", {})[kw_key] = deduped[-50:]

        # 가장 많이 나온 제목 찾기
        from collections import Counter
        title_counts = Counter(a["title"] for a in all_articles)
        best_title = title_counts.most_common(1)[0][0]
        best_article = next(a for a in all_articles if a["title"] == best_title)

        cache.setdefault("kw_articles", {})[kw_key] = {
            "title":   best_article["title"],
            "url":     best_article["url"],
            "source":  best_article.get("source", ""),
            "count":   total_count,
            "group":   articles[0]["group"],
            "keyword": articles[0]["keyword"],
        }


def get_all_news(cache: dict) -> list:
    """3건 이상 수집된 키워드만 반환."""
    article_pool = cache.get("kw_article_pool", {})
    kw_counts    = cache.get("kw_counts", {})
    if not article_pool:
        return []

    result = []
    for kw_key, articles in article_pool.items():
        if not articles:
            continue
        count = kw_counts.get(kw_key, len(articles))
        if count < 3:  # 3건 미만 제외
            continue
        group   = articles[0].get("group", "")
        keyword = articles[0].get("keyword", "")
        result.append({
            "group":    group,
            "keyword":  keyword,
            "count":    count,
            "articles": articles,
        })

    result.sort(key=lambda x: -x["count"])
    return result


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
        kst_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%m-%d %H:%M')
        lines = [f"📊 시황 {kst_str} KST\n"]
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


def format_news_message(groups: list) -> list:
    """키워드별 전체 기사 목록 포맷. 4096자 초과 시 분할."""
    if not groups:
        return []

    now_str = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M (KST)")
    messages = []
    current  = f"📰 뉴스 ({now_str})\n"

    for grp in groups:
        group   = grp["group"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        kw      = grp["keyword"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        count   = grp["count"]
        articles = grp["articles"]

        block = f"\n[{group}] {kw} {count}건\n"
        for art in articles:
            title = art.get("title", "")
            # 발행처 중복 제거
            parts = title.split(" - ")
            if len(parts) >= 3 and parts[-1].strip() == parts[-2].strip():
                parts = parts[:-1]
            if len(parts) >= 2:
                title = " - ".join(parts[:-1]).strip()
            else:
                title = parts[0].strip()

            source = clean_source(art.get("source", ""))
            url    = art.get("url", "")
            title  = title.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            src_str = f" - {source}" if source else ""
            block += f'  └ <a href="{url}">{title}{src_str}</a>\n'

        if len(current) + len(block) > 4000:
            messages.append(current)
            current = f"📰 뉴스 ({now_str}) (계속)\n{block}"
        else:
            current += block

    if current.strip():
        messages.append(current)
    return messages


def should_send_top10(cache: dict) -> bool:
    """1시간마다 TOP 10 전송 여부."""
    last = cache.get("last_top10_sent", "")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    now_dt  = datetime.now(timezone.utc)
    return (now_dt - last_dt).total_seconds() >= 3600


# ──────────────── 메인 ────────────────
def run_news():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 뉴스봇 시작")

    # KST 22:00~07:00 사이엔 실행 안 함
    kst_now  = datetime.now(timezone.utc) + timedelta(hours=9)
    kst_hour = kst_now.hour
    if kst_hour >= 22 or kst_hour < 7:
        print(f"[종료] KST {kst_hour}시 - 운영 시간 외 (07:00~22:00)")
        return

    # 1. 명령어 처리
    handle_commands()

    # 2. 캐시 로드 + 정리
    cache = load_cache()
    cache = clean_old_cache(cache)

    # 3. 시황 (매일 07:00~07:20 KST)
    if kst_hour == 7 and kst_now.minute <= 20:
        summary = fetch_market_summary()
        if summary:
            send_telegram(summary, parse_mode="")
            print("시황 전송 완료")

    # 4. 뉴스 수집 + 카운팅 (매 1분, 전송 안 함)
    keywords = load_keywords()
    if not keywords:
        print("키워드 없음 - 종료")
        return

    print(f"키워드 {sum(len(v) for v in keywords.values())}개 수집 중...")
    all_news = fetch_all_news(keywords)
    print(f"수집: {len(all_news)}건")

    process_news(all_news, cache)
    print("카운팅 완료")

    # 5. 1시간마다 수집된 뉴스 전송
    if should_send_top10(cache):
        groups = get_all_news(cache)
        if groups:
            messages = format_news_message(groups)
            for msg in messages:
                send_telegram(msg)
                time.sleep(1)
            print(f"뉴스 {len(groups)}개 그룹 전송 완료")
        else:
            print("수집된 뉴스 없음 - 전송 안 함")
        # 리셋
        cache["kw_counts"]       = {}
        cache["kw_articles"]     = {}
        cache["kw_article_pool"] = {}
        cache["last_top10_sent"] = datetime.now(timezone.utc).isoformat()

    save_cache(cache)
    print("완료")


if __name__ == "__main__":
    run_news()
