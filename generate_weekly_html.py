import datetime
import os
import re
import sys
import unicodedata
from zoneinfo import ZoneInfo

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "export_control_news.csv")
OUTPUT_PATH = os.path.join(BASE_DIR, "weekly_news_raw.txt")
JST = ZoneInfo("Asia/Tokyo")

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or ""))

def resolve_period():
    today = datetime.datetime.now(JST).date()
    # 直前の月曜〜日曜
    last_monday = today - datetime.timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + datetime.timedelta(days=6)
    return last_monday, last_sunday

def extract_weekly_data():
    last_monday, last_sunday = resolve_period()
    monday_str = last_monday.strftime("%Y-%m-%d")
    sunday_str = last_sunday.strftime("%Y-%m-%d")

    print(f"集計対象期間: {monday_str} 〜 {sunday_str}")

    if not os.path.exists(CSV_PATH):
        print(f"エラー: {CSV_PATH} が見つかりません。")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH, dtype=str, encoding="utf-8-sig")
    df["date_str"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date_str", "news_title"])

    mask = (df["date_str"] >= monday_str) & (df["date_str"] <= sunday_str)
    week_df = df[mask].copy()

    week_df["_key"] = week_df["news_title"].map(lambda t: re.sub(r"[\s\W_]+", "", nfkc(t)).lower())
    week_df = week_df.drop_duplicates(subset=["_key"]).drop(columns=["_key"])
    week_df = week_df.sort_values(by=["date_str", "news_source"], ascending=[False, True])

    print(f"抽出件数: {len(week_df)} 件")

    lines = [f"【対象期間: {monday_str} 〜 {sunday_str}】（総件数: {len(week_df)}件）", ""]
    for _, row in week_df.iterrows():
        url = str(row.get("news_url", "") or "").strip()
        suffix = f" {url}" if url and url != "nan" else ""
        lines.append(f"- [{row['date_str']}] {row['news_title']} ({row['news_source']}){suffix}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"成功: {OUTPUT_PATH} にデータを出力しました。")

if __name__ == "__main__":
    extract_weekly_data()