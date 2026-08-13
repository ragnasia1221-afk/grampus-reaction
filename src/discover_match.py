"""
グランパスの「直近の消化済み試合」と、それに対応するドメサカブログ記事を自動検出する。
GitHub Actionsでの毎日自動実行時、対象試合を人手で指定しなくて済むようにするための入口。

jleague.jp: club/{slug}/day/ ページに埋め込まれた RSC チャンクの中に
  gameLogInClubByYear: { "<シーズン区分>": [ {gameId, gameDate, opponentTeamShortName, ...}, ... ] }
というそのクラブの全消化試合ログがあるので、そこから gameId が最大(=最新)のものを取り出し、
試合結果ページURL (/match/j1/{year}/{code}/) を組み立てる。

ドメサカブログ: WordPress製なので、標準のWP REST API (/wp-json/wp/v2/posts?search=...) で
チーム名を含む最新記事を検索できる。念のため、記事の投稿日が試合日から離れすぎていないか
(≒本当にその試合の記事かどうか)を簡易チェックする。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import requests

from jleague_scraper import USER_AGENT, _extract_next_f_chunks, _walk


@dataclass
class LatestMatch:
    game_id: int
    url: str
    game_date: str  # サイト表記そのまま (例: "26/8/8")
    opponent: str


@dataclass
class BlogArticle:
    title: str
    url: str
    published_at: str  # ISO8601


def find_latest_completed_match(
    team_slug: str = "nagoya", timeout: int = 20, league_name: str | None = "明治安田Ｊ１"
) -> LatestMatch:
    """
    league_name: gameLogInClubByYear の各試合エントリには "leagueName" フィールドがあり、
    J1リーグ戦以外(ACL・天皇杯・シーズン前の"Ｊ１百年構想"など)の試合も同じログに混在している。
    指定した場合はこの値と完全一致する試合だけを対象にする(既定はJ1リーグ戦のみ)。
    Noneを渡すと従来通りリーグ種別を問わず最新の試合を対象にする。

    既知の不具合: G大阪(gosaka)を個別に処理すると、直近消化試合がACLプレーオフ扱いになり
    J1試合ページのパースに失敗していた(浦和側から処理すれば正しく取れていたので実害は
    無かったが、run_batch.py実行時にエラーログが出ていた)。この絞り込みで解消される。
    """
    url = f"https://www.jleague.jp/club/{team_slug}/day/"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    chunks = _extract_next_f_chunks(resp.text)

    games: list[dict] = []
    for chunk in chunks.values():
        for node, _ in _walk(chunk):
            if isinstance(node, dict) and "gameLogInClubByYear" in node:
                log = node["gameLogInClubByYear"]
                if isinstance(log, dict):
                    for season_games in log.values():
                        if isinstance(season_games, list):
                            games.extend(
                                g for g in season_games if isinstance(g, dict) and "gameId" in g
                            )

    if league_name is not None:
        games = [g for g in games if g.get("leagueName") == league_name]

    if not games:
        raise RuntimeError(f"{url} から試合ログ(gameLogInClubByYear)が見つかりませんでした。")

    latest = max(games, key=lambda g: g["gameId"])
    game_id = latest["gameId"]
    year = game_id // 1_000_000
    code = str(game_id)[4:]
    match_url = f"https://www.jleague.jp/match/j1/{year}/{code}/"

    return LatestMatch(
        game_id=game_id,
        url=match_url,
        game_date=latest.get("gameDate", ""),
        opponent=latest.get("opponentTeamShortName", ""),
    )


def find_blog_article(
    keyword: str = "名古屋",
    blog_base: str = "https://blog.domesoccer.jp",
    match_date: dt.date | None = None,
    max_date_gap_days: int = 3,
    timeout: int = 20,
) -> BlogArticle | None:
    """
    WordPress REST API でキーワードを含む最新記事を検索する。
    match_date を渡した場合、記事の投稿日がその前後 max_date_gap_days 日以内でなければ
    (=別の話題の記事の可能性が高いので) None を返す。
    """
    resp = requests.get(
        f"{blog_base}/wp-json/wp/v2/posts",
        params={"search": keyword, "per_page": 5, "orderby": "date", "order": "desc"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    posts = resp.json()
    if not posts:
        return None

    top = posts[0]
    published_at = top["date"]  # 例: "2026-08-08T21:55:21" (サイトのローカル時刻表記)
    if match_date is not None:
        article_date = dt.date.fromisoformat(published_at[:10])
        if abs((article_date - match_date).days) > max_date_gap_days:
            return None

    return BlogArticle(
        title=_strip_html_entities(top["title"]["rendered"]),
        url=top["link"],
        published_at=published_at,
    )


def _strip_html_entities(text: str) -> str:
    import html

    return html.unescape(text)


if __name__ == "__main__":
    match = find_latest_completed_match()
    print(f"[jleague] 最新消化試合: {match.game_date} vs {match.opponent}  (gameId={match.game_id})")
    print(f"  {match.url}")

    # gameDate は "26/8/8" のような表記なので試合日として解釈する
    y, m, d = match.game_date.split("/")
    match_date = dt.date(2000 + int(y), int(m), int(d))

    article = find_blog_article(match_date=match_date)
    if article:
        print(f"\n[ドメサカブログ] {article.title}")
        print(f"  {article.url}  ({article.published_at})")
    else:
        print("\n[ドメサカブログ] 対応する記事が見つかりませんでした。")
