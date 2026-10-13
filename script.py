import datetime
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request  # ← ここに追加します

from bs4 import BeautifulSoup
import feedparser
import pandas as pd
import requests

# 保存先はスクリプトと同じフォルダ
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "export_control_news.csv")
RETENTION_DAYS = 90
BACKFILL = os.environ.get("BACKFILL", "0") == "1"
SEARCH_WINDOW = "when:30d" if BACKFILL else "when:7d"
REQUEST_INTERVAL_SEC = 0.5
COLUMNS = ["date", "news_title", "news_source", "news_url"]
REQUIRED_COLUMNS = ["date", "news_title", "news_source"]

# ---------------------------------------------------------------
# 1. 指定機関・公式フィードの直接取得設定
# ---------------------------------------------------------------
DIRECT_FEEDS = [
    {
        "name": "OFAC (Recent Actions)",
        "type": "rss",
        "url": "https://ofac.treasury.gov/recent-actions.xml"
    },
    {
        "name": "CSIS (Press Releases)",
        "type": "html",
        "url": "https://www.csis.org/about/media/press-releases",
        "selector": "a.link, .view-content h3 a, main a"
    },
    {
        "name": "CISTEC (新着情報)",
        "type": "html",
        "url": "https://www.cistec.or.jp/",
        "selector": "a"
    },
    {
        "name": "JETRO (安全保障貿易管理)",
        "type": "html",
        "url": "https://www.jetro.go.jp/world/security_trade_control/",
        "selector": "a"
    },
    {
        "name": "Federal Register (BIS/Export Controls)",
        "type": "rss",
        "url": "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bagencies%5D%5B%5D=industry-and-security-bureau"
    }
]

# ---------------------------------------------------------------
# 2. 検索クエリ（Google News RSS：補助的な報道用）
# ---------------------------------------------------------------
KEYWORDS_EN = [
    "Bureau of Industry and Security", "BIS regulation", "export controls",
    "Entity List", "Foreign Direct Product Rule", "chip export controls",
    "sanctions", "economic security", "CFIUS", "supply chain resilience",
]
KEYWORDS_JA = [
    "輸出管理", "BIS規制", "エンティティリスト", "経済安全保障",
    "外国為替及び外国貿易法", "外為法", "みなし輸出", "経済安保",
    "半導体規制", "CISTEC", "JETRO", "OFAC", "CSIS",
]

QUERIES = []
for kw in KEYWORDS_JA:
    QUERIES.append((f"JA: {kw}", f"{kw} {SEARCH_WINDOW}", "ja", "JP", "JP:ja"))
for kw in KEYWORDS_EN:
    QUERIES.append((f"US: {kw}", f"{kw} {SEARCH_WINDOW}", "en-US", "US", "US:en"))

ALLOWED_SOURCES = [
    "日本経済新聞", "日経", "読売新聞", "朝日新聞", "毎日新聞", "産経", "NHK",
    "ロイター", "Reuters", "ブルームバーグ", "Bloomberg", "時事", "共同通信",
    "CISTEC", "JETRO", "CSIS", "Federal Register", "OFAC",
]

# 関連判定パターン（輸出管理・経済安保に関連するキーワード）
SECURITY_PATTERN = re.compile(
    r"(輸出管理|BIS|Bureau of Industry and Security|Entity List|"
    r"エンティティリスト|外国為替|外為法|経済安全保障|経済安保|制裁|"
    r"Sanctions|Export Control|CFIUS|禁輸|半導体規制|Supply Chain|"
    r"CISTEC|JETRO|USCC|OFAC|DDTC|CSIS|NSA)",
    re.IGNORECASE,
)

# 【新規追加】市場調査・SEO自動生成記事などのノイズを除外するためのパターン
NOISE_PATTERN = re.compile(
    r"(CAGR|年平均成長率|市場予測|市場の成長|市場シェア|詳細分析|世界市場の主要プレイヤー|"
    r"リサーチレポート|市場規模は成長を続け|プロセス液体制御|エッチングガス|圧力センサー|"
    r"ダイシングダイアタッチフィルム|プローブカード|IPコア|ICソケット)",
    re.IGNORECASE,
)

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")

def clean_title(raw: str) -> str:
    title = nfkc(raw)
    title = re.sub(r"\s+[-–|]\s+[^-–|]+$", "", title)
    title = re.sub(r"\s*[(（][^()（）]+[)）]\s*$", "", title)
    return title.strip()

def normalize_key(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", nfkc(title)).lower()

def is_relevant(entry_title: str, entry_summary: str = "") -> bool:
    text = nfkc(f"{entry_title} {entry_summary}")
    # 1. ノイズパターンに一致する場合は強制除外
    if NOISE_PATTERN.search(text):
        return False
    # 2. セキュリティ・輸出管理関連パターンに一致するか
    return bool(SECURITY_PATTERN.search(text))

import urllib.request

def fetch_direct_sources():
    """CISTEC, OFAC, CSIS, JETROなどの公式サイトを直接スクレイピング・RSS取得する"""
    rows = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    print("== 公式サイト・機関の直接巡回状況 ==")
    for feed in DIRECT_FEEDS:
        name = feed["name"]
        url = feed["url"]
        try:
            if feed["type"] == "rss":
                parsed = feedparser.parse(url)
                print(f"  {name} (RSS): {len(parsed.entries)}件")
                for entry in parsed.entries:
                    title = clean_title(entry.title)
                    link = getattr(entry, "link", "") or ""
                    
                    if title and is_relevant(title, getattr(entry, "summary", "")):
                        if getattr(entry, "published_parsed", None):
                            pub = datetime.datetime(*entry.published_parsed[:6]) + datetime.timedelta(hours=9)
                            date_str = pub.strftime("%Y-%m-%d")
                        else:
                            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
                        
                        rows.append({
                            "date": date_str,
                            "news_title": title,
                            "news_source": name,
                            "news_url": link
                        })
            elif feed["type"] == "html":
                # CISTEC専用の文字化け完全解消ロジック
                if "cistec.or.jp" in url:
                    try:
                        req = urllib.request.Request(url, headers=headers)
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            raw_bytes = resp.read()
                        # CISTECはWindows-31J (cp932) でエンコードされているため強制適用
                        html_text = raw_bytes.decode("cp932", errors="ignore")
                    except Exception as e:
                        print(f"  {name} urllib取得エラー: {e}")
                        continue
                else:
                    resp = requests.get(url, headers=headers, timeout=15)
                    if resp.status_code != 200:
                        print(f"  {name} (HTML): HTTP Status {resp.status_code}")
                        continue
                    encoding = resp.apparent_encoding if resp.apparent_encoding else "utf-8"
                    html_text = resp.content.decode(encoding, errors="replace")
                
                soup = BeautifulSoup(html_text, "html.parser")
                elements = soup.select(feed["selector"])
                print(f"  {name} (HTML): {len(elements)}要素取得")
                
                parsed_count = 0
                base_domain = "/".join(url.split("/")[:3])
                
                for el in elements:
                    raw_text = el.get_text()
                    title = clean_title(raw_text)
                    href = el.get("href", "")
                    if not title or len(title) < 3 or not href:
                        continue
                    
                    if not is_relevant(title):
                        continue
                    
                    if href.startswith("http"):
                        link = href
                    elif href.startswith("/"):
                        link = base_domain + href
                    else:
                        link = base_domain + "/" + href
                        
                    if any(skip in title.lower() for skip in ["home", "menu", "contact", "sitemap", "プライバシー"]):
                        continue
                        
                    rows.append({
                        "date": datetime.datetime.now().strftime("%Y-%m-%d"),
                        "news_title": title,
                        "news_source": name,
                        "news_url": link
                    })
                    parsed_count += 1
                    if parsed_count >= 15:
                        break
        except Exception as e:
            print(f"  {name} 取得エラー: {e}")
        time.sleep(REQUEST_INTERVAL_SEC)
        
    return rows

def fetch_news():
    direct_entries = fetch_direct_sources()
    
    raw_entries = []
    for name, query, hl, gl, ceid in QUERIES:
        encoded = urllib.parse.quote(query)
        g_url = f"https://news.google.com/rss/search?q={encoded}&hl={hl}&gl={gl}&ceid={ceid}"
        feed = feedparser.parse(g_url)
        raw_entries.extend(feed.entries)
        time.sleep(REQUEST_INTERVAL_SEC)

    rows, seen = [], set()

    # 先に公式直接取得分を登録
    for item in direct_entries:
        key = normalize_key(item["news_title"])
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(item)

    # Google News分は厳格な関連判定＋ノイズフィルターを通す
    dropped_noise = 0
    for entry in raw_entries:
        title = clean_title(entry.title)
        summary = getattr(entry, "summary", "")
        
        if not is_relevant(title, summary):
            dropped_noise += 1
            continue

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
            "news_source": "Google News",
            "news_url": getattr(entry, "link", "") or "",
        })

    print(f"Google News ノイズ除外数: {dropped_noise} 件")
    print(f"今回最終取得総数: {len(rows)} 件")
    return rows

def load_existing() -> pd.DataFrame:
    if not os.path.exists(CSV_PATH):
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(CSV_PATH, dtype=str, encoding="utf-8-sig")
        return df[COLUMNS] if set(REQUIRED_COLUMNS).issubset(df.columns) else pd.DataFrame(columns=COLUMNS)
    except Exception:
        return pd.DataFrame(columns=COLUMNS)

def main():
    print("指定機関・輸出管理ニュースの収集を開始します...")
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