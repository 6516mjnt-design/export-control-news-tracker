import datetime
import os
import re
import time
import unicodedata
import urllib.parse

import feedparser
import pandas as pd

# 保存先はスクリプトと同じフォルダ
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "export_control_news.csv")
RETENTION_DAYS = 90  # 規制動向は長期的に追うため長めに設定
BACKFILL = os.environ.get("BACKFILL", "0") == "1"
SEARCH_WINDOW = "when:30d" if BACKFILL else "when:7d"
REQUEST_INTERVAL_SEC = 0.5
COLUMNS = ["date", "news_title", "news_source", "news_url"]
REQUIRED_COLUMNS = ["date", "news_title", "news_source"]

# ---------------------------------------------------------------
# 1. 米国政府機関などの公式RSSフィード（一次情報）
# ---------------------------------------------------------------
GOVERNMENT_FEEDS = [
    {
        "name": "Federal Register (BIS/Export Controls)",
        "url": "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bagencies%5D%5B%5D=industry-and-security-bureau"
    },
]

# ---------------------------------------------------------------
# 2. 検索クエリ（Google News RSS：メディア・速報報道用）
# ---------------------------------------------------------------
# 従来およびご要望の機関・レジーム・キーワードを網羅
KEYWORDS_EN = [
    # 米国政府機関・シンクタンク・制裁関連
    "Bureau of Industry and Security", "BIS regulation", "export controls",
    "Entity List", "Foreign Direct Product Rule", "chip export controls",
    "sanctions", "economic security", "CFIUS", "supply chain resilience",
    "unverified list", "military end-use",
    "USCC", "U.S.-China Economic and Security Review Commission",
    "OFAC", "Office of Foreign Assets Control",
    "DDTC", "Directorate of Defense Trade Controls",
    "CSIS", "Center for Strategic and International Studies",
    "NSA",
    # 国際レジーム・条約
    "Australia Group", "Wassenaar Arrangement",
    "Missile Technology Control Regime", "MTCR",
    "Nuclear Suppliers Group", "NSG",
    "CWC", "Chemical Weapons Convention", "OPCW",
]

KEYWORDS_JA = [
    # 基本キーワード
    "輸出管理", "BIS規制", "エンティティリスト", "経済安全保障",
    "外国為替及び外国貿易法", "外為法", "みなし輸出", "経済安保",
    "半導体規制", "安全保障貿易", "禁輸措置",
    # 追加の日本機関
    "CISTEC", "安全保障貿易情報センター",
    "JETRO", "日本貿易振興機構",
    # 追加の米国機関・シンクタンク
    "米中経済安全保障調査委員会", "OFAC", "DDTC", "CSIS", "NSA",
    # 国際レジーム
    "オーストラリアグループ", "ワッセナー・アレンジメント",
    "ミサイル技術管理レジメン", "原子力供給国グループ",
    "化学兵器禁止機関",
]

QUERIES = []
for kw in KEYWORDS_JA:
    QUERIES.append((f"JA: {kw}", f"{kw} {SEARCH_WINDOW}", "ja", "JP", "JP:ja"))
for kw in KEYWORDS_EN:
    QUERIES.append((f"US: {kw}", f"{kw} {SEARCH_WINDOW}", "en-US", "US", "US:en"))
    QUERIES.append((f"UK: {kw}", f"{kw} {SEARCH_WINDOW}", "en-GB", "GB", "GB:en"))

# 許可する主な媒体名・政府機関など
ALLOWED_SOURCES = [
    # 日本
    "日本経済新聞", "日経", "読売新聞", "朝日新聞", "毎日新聞", "産経", "NHK",
    "ロイター", "Reuters", "ブルームバーグ", "Bloomberg", "時事", "共同通信",
    "JBpress", "東洋経済", "日経クロステック", "ITmedia", "CISTEC", "JETRO",
    # 米国・海外
    "Federal Register", "Reuters", "Bloomberg", "Wall Street Journal", "The New York Times",
    "The Washington Post", "Financial Times", "Politico", "POLITICO", "Axios",
    "CNBC", "BBC", "The Guardian", "The Telegraph", "CSIS",
]

# 関連判定パターン（追加した機関やレジームの略称・名称も幅広くカバー）
SECURITY_PATTERN = re.compile(
    r"(輸出管理|BIS|Bureau of Industry and Security|Entity List|"
    r"エンティティリスト|外国為替|外為法|経済安全保障|経済安保|制裁|"
    r"Sanctions|Export Control|CFIUS|禁輸|半導体規制|Supply Chain|"
    r"FDPR|Foreign Direct Product|"
    r"CISTEC|JETRO|USCC|OFAC|DDTC|CSIS|NSA|"
    r"Australia Group|Wassenaar|MTCR|Nuclear Suppliers Group|OPCW|"
    r"オーストラリアグループ|ワッセナー|原子力供給国グループ)",
    re.IGNORECASE,
)

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")

def get_google_news_rss(query: str, hl: str, gl: str, ceid: str):
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl={hl}&gl={gl}&ceid={ceid}"
    return feedparser.parse(url)

def clean_title(raw: str) -> str:
    title = nfkc(raw)
    title = re.sub(r"\s+[-–|]\s+[^-–|]+$", "", title)
    title = re.sub(r"\s*[(（][^()（）]+[)）]\s*$", "", title)
    return title.strip()

SOURCE_ALIASES = [
    ("日本経済新聞", "日本経済新聞"),
    ("日経クロステック", "日経クロステック"),
    ("NHK", "NHK"),
    ("時事", "時事通信"),
    ("産経", "産経新聞"),
    ("Bloomberg", "Bloomberg"),
    ("Reuters", "Reuters"),
    ("ロイター", "Reuters"),
    ("CNN", "CNN"),
    ("Politico", "Politico"),
    ("Federal Register", "Federal Register"),
    ("CSIS", "CSIS"),
]

def canonical_source(name: str) -> str:
    for pattern, canonical in SOURCE_ALIASES:
        if pattern.lower() in name.lower():
            return canonical
    return name

def source_name(entry) -> str:
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return canonical_source(src.title.strip())
    m = re.search(r"(?:\s[-–|]\s|[(（])([^-–|()（）]+)[)）]?\s*$", nfkc(entry.title))
    return canonical_source(m.group(1).strip()) if m else "不明"

def is_allowed_source(name: str) -> bool:
    low = name.lower()
    return any(allowed.lower() in low for allowed in ALLOWED_SOURCES)

def is_relevant(entry) -> bool:
    text = nfkc(f"{entry.title} {getattr(entry, 'summary', '')}")
    return bool(SECURITY_PATTERN.search(text))

def normalize_key(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", nfkc(title)).lower()

def fetch_government_rss():
    """米国政府機関等の公式RSSから直接告示・プレスリリースを取得する"""
    gov_rows = []
    print("== 政府機関公式RSSの取得状況 ==")
    for feed_info in GOVERNMENT_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            n = len(feed.entries)
            print(f"  {feed_info['name']}: {n}件")
            for entry in feed.entries:
                title = clean_title(entry.title)
                url = getattr(entry, "link", "") or ""
                
                if getattr(entry, "published_parsed", None):
                    pub = datetime.datetime(*entry.published_parsed[:6]) + datetime.timedelta(hours=9)
                    date_str = pub.strftime("%Y-%m-%d")
                else:
                    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
                
                gov_rows.append({
                    "date": date_str,
                    "news_title": title,
                    "news_source": feed_info["name"],
                    "news_url": url,
                })
        except Exception as e:
            print(f"政府RSS取得エラー ({feed_info['name']}): {e}")
        time.sleep(REQUEST_INTERVAL_SEC)
    return gov_rows

def fetch_news():
    raw_entries = []
    
    # 1. 政府機関の公式フィードから直接取得
    gov_entries = fetch_government_rss()
    
    # 2. Google News RSSからメディア報道を取得
    print("== 取得状況（Google News クエリごと） ==")
    for name, query, hl, gl, ceid in QUERIES:
        feed = get_google_news_rss(query, hl, gl, ceid)
        n = len(feed.entries)
        status = "OK" if n else "0件"
        print(f"  {name}: {n}件 ({status})")
        raw_entries.extend(feed.entries)
        time.sleep(REQUEST_INTERVAL_SEC)
    print(f"Google News 取得総数（重複前）: {len(raw_entries)} 件")

    rows, seen = [], set()
    dropped_count = 0

    # 先に政府機関公式フィード分を処理
    for item in gov_entries:
        key = normalize_key(item["news_title"])
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(item)

    # 次にGoogle News分をフィルタリングして追加
    for entry in raw_entries:
        source = source_name(entry)
        if not is_allowed_source(source) or not is_relevant(entry):
            dropped_count += 1
            continue

        title = clean_title(entry.title)
        key = normalize_key(title)
        if not key or key in seen:
            continue
        seen.add(key)

        if getattr(entry, "published_parsed", None):
            pub = datetime.datetime(*entry.published_parsed[:6]) + datetime.timedelta(hours=9)
            date_str = pub.strftime("%Y-%m-%d")
        else:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")

        rows.append({
            "date": date_str,
            "news_title": title,
            "news_source": source,
            "news_url": getattr(entry, "link", "") or "",
        })

    print(f"Google News 条件外のため除外: {dropped_count} 件")
    print(f"今回最終取得総数: {len(rows)} 件（政府公式含む）")
    return rows

def load_existing() -> pd.DataFrame:
    if not os.path.exists(CSV_PATH):
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(CSV_PATH, dtype=str, encoding="utf-8-sig")
        if not set(REQUIRED_COLUMNS).issubset(df.columns):
            return pd.DataFrame(columns=COLUMNS)
        if "news_url" not in df.columns:
            df["news_url"] = ""
        return df[COLUMNS]
    except Exception:
        return pd.DataFrame(columns=COLUMNS)

def main():
    print("輸出管理・経済安保ニュースの取得を開始します...")
    new_rows = fetch_news()
    df_new = pd.DataFrame(new_rows, columns=COLUMNS)
    df_old = load_existing()

    df = pd.concat([df_old, df_new], ignore_index=True)
    df["_key"] = df["news_title"].fillna("").map(normalize_key)
    df.drop_duplicates(subset=["date", "_key"], keep="last", inplace=True)
    df.drop(columns=["_key"], inplace=True)

    df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")
    cutoff = datetime.datetime.now() - datetime.timedelta(days=RETENTION_DAYS)
    df = df[df["date_dt"] >= cutoff].drop(columns=["date_dt"])

    df.sort_values(by=["date", "news_source"], ascending=[False, True], inplace=True)
    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    print(f"保存しました: {CSV_PATH}（{len(df)} 行）")

if __name__ == "__main__":
    main()