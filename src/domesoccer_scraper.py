"""
まとめブログ(ドメサカブログ等)の試合反応記事から、
- 掲示板コメント欄 (comment-list)
- 記事内に貼られた引用ツイート ("ツイッターの反応" 見出し以降の blockquote.twitter-tweet)
を取得する。

ドメサカブログはWordPress製の通常の静的HTML(jleague.jpと違ってJSレンダリング不要)なので、
requests + BeautifulSoup だけで完結する。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ツイッターの反応(ファンのツイート引用)は、この見出しテキストより後ろにあるものだけを対象にする。
# これより前に出てくる blockquote.twitter-tweet は、Jリーグ公式アカウントのハイライト動画ツイート等、
# 「試合反応」ではなく記事本文の資料として貼られたものなので除外する。
REACTION_SECTION_MARKER = "ツイッターの反応"

# コメント投稿者がアイコン横に自由入力するチームタグを、home/away/other に振り分けるための
# 簡易エイリアス表。完全一致ではなく「タグ文字列がチーム名に含まれる/チーム名がタグ文字列を含む」で判定する。
# 主要J1クラブのニックネームだけ最低限カバーしている。必要に応じて追加すること。
TEAM_NICKNAMES: dict[str, list[str]] = {
    # 各リストは「クラブ固有のニックネームを先頭」に並べている(地名を先頭にすると、
    # 5ch検索(run_pipeline.pyの_nichan_keyword_for_team)で無関係な「vs<地名>」スレ等の
    # 地名由来の誤ヒットを拾いやすいため。例:「名古屋」だけだと対戦相手側が立てた
    # 「しみじみ実況 vs名古屋」のようなスレともヒットしてしまう)。
    "名古屋グランパス": ["グランパス", "名古屋", "鯱"],
    "清水エスパルス": ["エスパルス", "清水"],
    "鹿島アントラーズ": ["アントラーズ", "鹿島", "鹿"],
    "浦和レッズ": ["レッズ", "浦和"],
    "柏レイソル": ["レイソル", "柏"],
    "FC東京": ["FC東京", "東京", "F東"],
    "東京ヴェルディ": ["ヴェルディ", "東京V"],
    "川崎フロンターレ": ["フロンターレ", "川崎"],
    "横浜F・マリノス": ["マリノス", "横浜FM", "横浜F"],
    "横浜FC": ["横浜FC"],
    "湘南ベルマーレ": ["ベルマーレ", "湘南"],
    "アルビレックス新潟": ["アルビレックス", "新潟"],
    "京都サンガF.C.": ["サンガ", "京都"],
    "ガンバ大阪": ["ガンバ", "G大阪"],
    "セレッソ大阪": ["セレッソ", "C大阪"],
    "ヴィッセル神戸": ["ヴィッセル", "神戸"],
    "ファジアーノ岡山": ["ファジアーノ", "岡山"],
    "サンフレッチェ広島": ["サンフレッチェ", "広島"],
    "アビスパ福岡": ["アビスパ", "福岡"],
    "町田ゼルビア": ["ゼルビア", "町田"],
    "サガン鳥栖": ["サガン", "鳥栖"],
}

# jleague.jpは全角英数(例:"横浜Ｆ・マリノス"の"Ｆ")を使うことがあり、
# 上のTEAM_NICKNAMES辞書のキー(半角)と表記が食い違うことがある。
# NFKC正規化してから照合することでこの表記ゆれを吸収する。
_TEAM_NICKNAMES_NORMALIZED = {
    unicodedata.normalize("NFKC", name): aliases for name, aliases in TEAM_NICKNAMES.items()
}


def get_team_aliases(team_name: str) -> list[str]:
    """team_nameの表記ゆれ(全角/半角など)を吸収してTEAM_NICKNAMESを引く。"""
    return _TEAM_NICKNAMES_NORMALIZED.get(unicodedata.normalize("NFKC", team_name), [])


@dataclass
class BoardComment:
    comment_id: str
    number: int | None
    team_tag: str          # 投稿者が選んだアイコン横のタグ文字列 (例: "清水", "鯱")
    affiliation: str       # "home" / "away" / "other" (home_team/away_team との照合結果)
    posted_at: str | None  # 例: "2026.8.8 21:56"
    text: str              # 改行込みの本文


@dataclass
class TweetQuote:
    author_name: str
    handle: str
    text: str
    url: str
    posted_at: str | None


@dataclass
class BlogReactions:
    url: str
    comments: list[BoardComment] = field(default_factory=list)
    tweets: list[TweetQuote] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "comments": [c.__dict__ for c in self.comments],
            "tweets": [t.__dict__ for t in self.tweets],
        }


def fetch_html(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _classify_affiliation(team_tag: str, home_team: str, away_team: str) -> str:
    tag = team_tag.strip()
    if not tag:
        return "other"

    def _matches(full_name: str) -> bool:
        if tag in full_name or full_name in tag:
            return True
        for alias in get_team_aliases(full_name):
            if tag in alias or alias in tag:
                return True
        return False

    if _matches(home_team):
        return "home"
    if _matches(away_team):
        return "away"
    return "other"


def _text_with_linebreaks(tag: Tag) -> str:
    """
    <br> を改行として、<img alt="絵文字"> を alt テキストとして展開してから
    タグ内の可視テキストを取り出す。(元のTagは破壊しないようコピーして操作する)

    get_text(separator) は子孫のテキストノードすべての"間"にセパレータを挿む仕様なので、
    <br>で分割された行の前後にある空文字ノードの分まで余計に"\n"が増殖してしまう。
    そのため最後に連続する空白行をまとめて1個の改行に正規化する。
    """
    tag = BeautifulSoup(str(tag), "lxml")
    for br in tag.find_all("br"):
        br.replace_with("\n")
    for img in tag.find_all("img"):
        img.replace_with(img.get("alt", ""))
    raw = tag.get_text("\n")
    # 空行(空白文字だけの行)が連続している箇所を1個の改行にまとめる
    lines = [line.strip() for line in raw.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


_DATE_PATTERN = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2}")


def _parse_comments(soup: BeautifulSoup, home_team: str, away_team: str) -> list[BoardComment]:
    comments: list[BoardComment] = []

    for li in soup.select('li[id^="li-comment-"]'):
        comment_id = li["id"].replace("li-comment-", "")

        number = None
        count_span = li.select_one(".comment_count")
        if count_span:
            m = re.search(r"\d+", count_span.get_text())
            if m:
                number = int(m.group())

        author_div = li.select_one(".comment-author")
        team_tag = ""
        if author_div:
            img = author_div.find("img")
            if img:
                img.extract()  # アイコン画像を除いた残りのテキストがチームタグ
            team_tag = author_div.get_text(strip=True)

        meta_div = li.select_one(".comment-meta")
        posted_at = None
        if meta_div:
            m = _DATE_PATTERN.search(meta_div.get_text())
            if m:
                posted_at = m.group()

        text_div = li.select_one(".comment-text")
        body = ""
        if text_div:
            paragraphs = text_div.find_all("p") or [text_div]
            body = "\n\n".join(
                _text_with_linebreaks(p).strip() for p in paragraphs
            ).strip()

        comments.append(
            BoardComment(
                comment_id=comment_id,
                number=number,
                team_tag=team_tag,
                affiliation=_classify_affiliation(team_tag, home_team, away_team),
                posted_at=posted_at,
                text=body,
            )
        )

    return comments


_ATTRIBUTION_PATTERN = re.compile(r"—\s*(?P<name>.+?)\s*\((?P<handle>[^)]+)\)\s*$")


def _parse_tweets(soup: BeautifulSoup) -> list[TweetQuote]:
    # "ツイッターの反応" 見出しを探し、それより後ろにある blockquote.twitter-tweet だけを対象にする。
    marker = soup.find(string=re.compile(REACTION_SECTION_MARKER))
    if marker is None:
        return []

    tweets: list[TweetQuote] = []
    for blockquote in marker.find_all_next("blockquote", class_="twitter-tweet"):
        p_tag = blockquote.find("p")
        body = _text_with_linebreaks(p_tag).strip() if p_tag else ""

        links = blockquote.find_all("a")
        if not links:
            continue
        permalink_a = links[-1]  # 最後の<a>がステータスへのパーマリンク(表示テキストは日付)
        tweet_url = permalink_a.get("href", "")
        posted_at = permalink_a.get_text(strip=True) or None

        # パーマリンクの直前にあるテキストノードに "— 表示名 (handle) " が入っている
        attribution_node = permalink_a.previous_sibling
        author_name, handle = "", ""
        if isinstance(attribution_node, NavigableString):
            m = _ATTRIBUTION_PATTERN.search(str(attribution_node))
            if m:
                author_name = m.group("name").strip()
                handle = m.group("handle").strip()

        if not handle:
            # フォールバック: パーマリンクURL (.../<handle>/status/<id>) から推測
            m = re.search(r"twitter\.com/([^/]+)/status/", tweet_url) or re.search(
                r"x\.com/([^/]+)/status/", tweet_url
            )
            if m:
                handle = m.group(1)

        tweets.append(
            TweetQuote(
                author_name=author_name,
                handle=handle,
                text=body,
                url=tweet_url,
                posted_at=posted_at,
            )
        )

    return tweets


def parse_reactions(html: str, url: str, home_team: str, away_team: str) -> BlogReactions:
    soup = BeautifulSoup(html, "lxml")
    return BlogReactions(
        url=url,
        comments=_parse_comments(soup, home_team, away_team),
        tweets=_parse_tweets(soup),
    )


def get_blog_reactions(url: str, home_team: str, away_team: str) -> BlogReactions:
    html = fetch_html(url)
    return parse_reactions(html, url=url, home_team=home_team, away_team=away_team)


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    target_url = (
        sys.argv[1] if len(sys.argv) > 1 else "https://blog.domesoccer.jp/archives/60252629.html"
    )
    # 前段(jleague_scraper.py)で保存したJSONがあれば home_team/away_team を引き継ぐ。
    match_json_path = Path(__file__).resolve().parent.parent / "data" / "match_result.json"
    home_team, away_team = "名古屋グランパス", "清水エスパルス"
    if match_json_path.exists():
        match_data = json.loads(match_json_path.read_text(encoding="utf-8"))
        home_team = match_data.get("home_team", home_team)
        away_team = match_data.get("away_team", away_team)

    reactions = get_blog_reactions(target_url, home_team=home_team, away_team=away_team)

    out_path = Path(__file__).resolve().parent.parent / "data" / "blog_reactions.json"
    out_path.write_text(
        json.dumps(reactions.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[saved] {out_path}")

    print("=" * 60)
    print(f"コメント数: {len(reactions.comments)}  /  ツイート数: {len(reactions.tweets)}")
    by_affiliation: dict[str, int] = {}
    for c in reactions.comments:
        by_affiliation[c.affiliation] = by_affiliation.get(c.affiliation, 0) + 1
    print("内訳:", by_affiliation)
    print("-" * 60)
    print("【コメント サンプル(先頭3件)】")
    for c in reactions.comments[:3]:
        print(f"  #{c.number} [{c.affiliation}/{c.team_tag}] {c.posted_at}")
        print(f"    {c.text[:60].replace(chr(10), ' / ')}...")
    print("-" * 60)
    print("【ツイート サンプル(先頭3件)】")
    for t in reactions.tweets[:3]:
        print(f"  {t.author_name} (@{t.handle})  {t.posted_at}")
        print(f"    {t.text[:60].replace(chr(10), ' / ')}...")
        print(f"    {t.url}")
    print("=" * 60)
