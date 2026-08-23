"""
ステップ1〜3で生成した
  data/match_result.json      (試合結果: jleague_scraper.py)
  data/blog_reactions.json    (掲示板コメント・引用ツイート: domesoccer_scraper.py)
  data/generated_article.json (まとめ文・引用選定: generate_article.py)
をもとに、テンプレート templates/nagoya_shimizu_reaction.html.j2 (Jinja2) を描画して
最終的なHTMLページを output/ に書き出す。
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import emblem

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TEMPLATE_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
DOCS_DIR = BASE_DIR / "docs"
EMBLEM_ASSETS_DIR = DOCS_DIR / "assets" / "emblems"

# エンブレム切り出しに失敗した場合の色つき円バッジのフォールバック色
# (クラブカラーが取れない場合はこの色を使う)
FALLBACK_HOME_COLOR = "#8c1d20"
FALLBACK_AWAY_COLOR = "#e8781f"

# 名前が取れない(匿名)投稿者向けの汎用バッジ文字。「名無しさん」の頭文字「名」を
# そのままバッジに出すとクラブ略称と紛らわしいため、これに差し替える。
ANONYMOUS_BADGE_ICON = "👤"

JST = timezone(timedelta(hours=9))
WEEKDAY_JA = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# 主要クラブの短縮呼称(バッジ下のラベルに使う)。ここに無いクラブは
# チーム名の先頭2文字を自動で使う(汎用フォールバック)。
TEAM_SHORT_NAMES: dict[str, str] = {
    "名古屋グランパス": "名古屋",
    "清水エスパルス": "清水",
    "鹿島アントラーズ": "鹿島",
    "浦和レッズ": "浦和",
    "柏レイソル": "柏",
    "FC東京": "FC東京",
    "東京ヴェルディ": "東京V",
    "川崎フロンターレ": "川崎",
    "横浜F・マリノス": "横浜FM",
    "横浜FC": "横浜FC",
    "湘南ベルマーレ": "湘南",
    "アルビレックス新潟": "新潟",
    "京都サンガF.C.": "京都",
    "ガンバ大阪": "G大阪",
    "セレッソ大阪": "C大阪",
    "ヴィッセル神戸": "神戸",
    "ファジアーノ岡山": "岡山",
    "サンフレッチェ広島": "広島",
    "アビスパ福岡": "福岡",
    "FC町田ゼルビア": "町田",  # jleague.jpの正式表記は「ＦＣ町田ゼルビア」(NFKC正規化後の半角表記をキーにしている)
    "サガン鳥栖": "鳥栖",
    "ジェフユナイテッド千葉": "千葉",
    "水戸ホーリーホック": "水戸",
    "V・ファーレン長崎": "長崎",  # jleague.jpの正式表記は「Ｖ・ファーレン長崎」
}


# jleague.jpは全角英数(例:"横浜Ｆ・マリノス"の"Ｆ")を使うことがあり、TEAM_SHORT_NAMES
# 辞書のキー(半角)と表記が食い違うことがあるため、NFKC正規化してから照合する。
_TEAM_SHORT_NAMES_NORMALIZED = {
    unicodedata.normalize("NFKC", name): short for name, short in TEAM_SHORT_NAMES.items()
}


def _team_short(team_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", team_name)
    return _TEAM_SHORT_NAMES_NORMALIZED.get(normalized, team_name[:2])


# 5ch/2ch・ファンサカ2ch辞典等で使われている、各クラブの一文字表記。バッジの文字に使う。
# マスコット/クラブカラー/クラブ名由来など出典がはっきりしていて角の立たないものだけを採用し、
# 蔑称や由来不明なものは避けている(2026-08-14 ユーザーと相談のうえ決定)。
# 由来が2文字以上のもの(柏レイソル「木白」、FC東京「瓦斯」、川崎フロンターレ「海豚」)は
# 一文字表記のルールに揃えるため、チーム名由来の一文字に置き換えている。
# 参考: https://w.atwiki.jp/fantasy_soccer/pages/12.html (ファンサカ2ch辞典)
CLUB_ONE_CHAR: dict[str, str] = {
    "名古屋グランパス": "鯱",  # grampus(グランパス)はシャチの意味
    "サンフレッチェ広島": "熊",  # クマのマスコットより
    "横浜F・マリノス": "鞠",  # marinos の「マリ」より
    "セレッソ大阪": "桜",  # cerezo(セレッソ)は桜の意味
    "アビスパ福岡": "蜂",  # avispa(アビスパ)は蜂の意味
    "鹿島アントラーズ": "鹿",  # 鹿島の頭文字 + antler(鹿の角)
    "東京ヴェルディ": "緑",  # 定番のクラブカラー
    "浦和レッズ": "赤",  # クラブカラー
    "清水エスパルス": "橙",  # クラブカラー
    "京都サンガF.C.": "紫",  # 旧称パープルサンガのクラブカラー
    "ヴィッセル神戸": "牛",  # 神戸牛ネタ
    "ガンバ大阪": "脚",
    "ジェフユナイテッド千葉": "犬",
    "柏レイソル": "柏",
    "FC東京": "東",
    "川崎フロンターレ": "川",
    "水戸ホーリーホック": "水",  # 該当スラング無し、地名からのフォールバック
    "ファジアーノ岡山": "岡",  # 同上
    "FC町田ゼルビア": "町",  # 同上
    "V・ファーレン長崎": "長",  # 同上(ファンサカ2ch辞典でも「未定」)
}

_CLUB_ONE_CHAR_NORMALIZED = {
    unicodedata.normalize("NFKC", name): char for name, char in CLUB_ONE_CHAR.items()
}


def _club_one_char(team_name: str) -> str:
    """バッジに使う一文字表記。未登録のクラブはチーム名の先頭一文字にフォールバックする。"""
    normalized = unicodedata.normalize("NFKC", team_name)
    return _CLUB_ONE_CHAR_NORMALIZED.get(normalized, team_name[0])


# 一文字 → クラブ名 の逆引き(「他クラブ」コメントの識別用)。
# CLUB_ONE_CHARの値はクラブごとに重複しないので単純な逆引きで作れる。
_ONE_CHAR_TO_CLUB = {char: name for name, char in CLUB_ONE_CHAR.items()}

_MATCH_META_RE = re.compile(
    r'<script type="application/json" id="match-meta">(.*?)</script>', re.S
)
_club_color_cache: dict[str, str] | None = None


def _club_color_reference() -> dict[str, str]:
    """docs/配下の全試合ページのmatch-metaから「クラブ名(NFKC正規化) → クラブカラー」の
    対応表を作る。「他クラブ」コメントを一文字表記からクラブ特定できた場合に、
    (今回の対戦カードには含まれない)そのクラブの実際の色をバッジに使うためのもの。
    プロセス内で一度だけ読み込んでキャッシュする。"""
    global _club_color_cache
    if _club_color_cache is not None:
        return _club_color_cache
    colors: dict[str, str] = {}
    if DOCS_DIR.exists():
        for f in DOCS_DIR.glob("*.html"):
            if f.name == "index.html":
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            m = _MATCH_META_RE.search(text)
            if not m:
                continue
            try:
                meta = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            for side in ("home", "away"):
                name = meta.get(f"{side}_team")
                color = meta.get(f"{side}_color")
                if name and color:
                    colors[unicodedata.normalize("NFKC", name)] = color
    _club_color_cache = colors
    return colors


def _format_kickoff(kickoff_iso: str | None) -> str:
    if not kickoff_iso:
        return ""
    dt_utc = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
    dt_jst = dt_utc.astimezone(JST)
    weekday = WEEKDAY_JA[dt_jst.weekday()]
    return f"{dt_jst:%Y.%m.%d} ({weekday}) {dt_jst:%H:%M}"


def _build_score_goal_line(match: dict) -> str:
    home_char = match["home_team"][0]
    away_char = match["away_team"][0]
    team_char = {match["home_team"]: home_char, match["away_team"]: away_char}

    goal_parts = [
        f"{g['player']}（{team_char.get(g['team'], g['team'][0])}）{g['minute']}"
        for g in match.get("goals", [])
    ]
    goals_text = "、".join(goal_parts) if goal_parts else "なし"

    # カードは同じチームの選手名をまとめて1グループにする
    by_team: dict[str, list[str]] = {}
    for c in match.get("cards", []):
        by_team.setdefault(c["team"], []).append(c["player"])
    # 同一選手の重複(同じ試合内で複数枚)は名前としては1回にまとめる
    card_groups = []
    for team, players in by_team.items():
        seen = list(dict.fromkeys(players))  # 順序を保った重複除去
        char = team_char.get(team, team[0])
        card_groups.append(f"{'、'.join(seen)}（{char}）")
    cards_text = "　".join(card_groups) if card_groups else "なし"

    return f"得点：{goals_text}　／　警告・退場：{cards_text}"


def _is_anonymous_name(name: str) -> bool:
    """ドメサカブログのコメント欄は「名無しさん」がデフォルトのタグ名で、
    これをそのまま先頭一文字だけ取り出すと「名」という紛らわしいバッジ文字になってしまう
    (クラブの略称と誤解される)。「名無し」で始まる場合は名前ではなく「未設定」の意味なので、
    バッジには文字ではなく汎用の人物アイコンを使う。"""
    return name.strip().startswith("名無し")


def _blog_source_name(url: str) -> str:
    if "domesoccer" in url:
        return "ドメサカブログ"
    return url.split("/")[2] if "://" in url else url


def build_context(
    match: dict,
    reactions: dict,
    article: dict,
    nichan: dict | None = None,
    focus_club: str | None = None,
    game_id: int | None = None,
) -> dict:
    home_short = _team_short(match["home_team"])
    away_short = _team_short(match["away_team"])
    home_badge_letter = _club_one_char(match["home_team"])
    away_badge_letter = _club_one_char(match["away_team"])

    # クラブエンブレム画像切り出し + クラブカラー取得。
    # スプライト画像のダウンロード等に失敗した場合はNoneを返す設計なので、
    # テンプレート側は emblem_src が無ければ従来通りの色つき円+文字表示にフォールバックする。
    home_visual = match.get("home_visual")
    away_visual = match.get("away_visual")
    home_emblem_src = emblem.get_emblem_relative_path(home_visual, EMBLEM_ASSETS_DIR)
    away_emblem_src = emblem.get_emblem_relative_path(away_visual, EMBLEM_ASSETS_DIR)
    home_color = (home_visual or {}).get("primary_color") or FALLBACK_HOME_COLOR
    away_color = (away_visual or {}).get("primary_color") or FALLBACK_AWAY_COLOR

    # focus_club(このサイトが応援するクラブ)がこの試合のどちら側かによって、
    # セクション見出しの言い回しを「ファン視点」/「中立視点」で出し分ける。
    # (NFKC正規化で全角/半角の表記ゆれを吸収する。例: jleague.jpの"横浜Ｆ・マリノス"の全角Ｆ)
    def _norm(s: str | None) -> str:
        return unicodedata.normalize("NFKC", s) if s else ""

    if focus_club is not None and _norm(focus_club) == _norm(match["home_team"]):
        fan_side = "home"
    elif focus_club is not None and _norm(focus_club) == _norm(match["away_team"]):
        fan_side = "away"
    else:
        fan_side = None

    def _section_title(team_short: str, side: str) -> str:
        if fan_side is None or fan_side == side:
            return f"掲示板の反応：{team_short}サポーター"
        return f"掲示板の反応：{team_short}サポーター（対比として）"

    home_section_title = _section_title(home_short, "home")
    away_section_title = _section_title(away_short, "away")

    comments_by_id = {c["comment_id"]: c for c in reactions.get("comments", [])}
    tweets_by_url = {t["url"]: t for t in reactions.get("tweets", [])}

    home_comments = []
    away_comments = []
    for pick in article.get("board_picks", []):
        source = comments_by_id.get(pick["comment_id"])
        if source is None:
            continue  # 元データに無いものは(検証済みのはずだが)念のためスキップ

        if pick["affiliation"] == "home":
            entry = {
                **pick,
                "number": source.get("number"),
                "badge_letter": home_badge_letter,
                "badge_color": home_color,
                "emblem_src": home_emblem_src,
                "tag_label": home_short,
            }
            home_comments.append(entry)
        elif pick["affiliation"] == "away":
            entry = {
                **pick,
                "number": source.get("number"),
                "badge_letter": away_badge_letter,
                "badge_color": away_color,
                "emblem_src": away_emblem_src,
                "tag_label": away_short,
            }
            away_comments.append(entry)
        else:  # other
            raw_tag = (source.get("team_tag") or "他").strip()
            if _is_anonymous_name(raw_tag):
                badge_letter = ANONYMOUS_BADGE_ICON
                badge_color = None
            else:
                badge_letter = raw_tag[0] if raw_tag else "他"
                # 一文字表記(CLUB_ONE_CHAR)から他クラブを特定できた場合は、そのクラブの
                # 実際のクラブカラーをバッジに使う(追加ルール)。特定できなければ
                # Noneのままテンプレート側でCSSの--other(グレー)にフォールバックする。
                other_club = _ONE_CHAR_TO_CLUB.get(badge_letter)
                badge_color = None
                if other_club:
                    badge_color = _club_color_reference().get(
                        unicodedata.normalize("NFKC", other_club)
                    )
            entry = {
                **pick,
                "number": source.get("number"),
                "badge_letter": badge_letter,
                "badge_color": badge_color,
                "emblem_src": None,  # 他クラブはエンブレム画像を使わない(色付けのみ)
                "tag_label": "他クラブ",
            }
            home_comments.append(entry)  # テンプレート原案どおり、他クラブ視点は名古屋側セクションに混ぜる

    tweets = []
    for pick in article.get("tweet_picks", []):
        source = tweets_by_url.get(pick["tweet_url"])
        if source is None:
            continue
        author_name = source.get("author_name") or source.get("handle") or "?"
        if _is_anonymous_name(author_name):
            avatar_letter = ANONYMOUS_BADGE_ICON
        else:
            avatar_letter = author_name[0] if author_name else "?"
        tweets.append(
            {
                **pick,
                "author_name": author_name,
                "handle": source.get("handle", ""),
                "avatar_letter": avatar_letter,
            }
        )

    nichan_posts_by_number = {p["number"]: p for p in (nichan or {}).get("posts", [])}
    nichan_picks = []
    for pick in article.get("nichan_picks", []):
        source = nichan_posts_by_number.get(pick["post_number"])
        if source is None:
            continue
        nichan_picks.append({**pick, "posted_at": source.get("posted_at", "")})

    headline = f"{match['home_team']} {match['home_score']}-{match['away_score']} {match['away_team']}<br>掲示板・Xの反応まとめ"

    blog_url = reactions.get("url", "")
    nichan_thread_title = (nichan or {}).get("title", "")

    # ページ内に埋め込む構造化メタデータ。一覧ページ・前後の試合ナビゲーションは
    # このJSONを全ページから収集して後処理(run_pipeline.update_index)で組み立てる。
    # (ファイル名やtitleタグの文字列パースに頼らない、自己記述的な設計)
    match_meta = {
        "game_id": game_id,
        "section": match.get("section", ""),
        "kickoff_iso": match.get("kickoff_iso"),
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "home_score": match["home_score"],
        "away_score": match["away_score"],
        "home_short": home_short,
        "away_short": away_short,
        "home_emblem_src": home_emblem_src,
        "away_emblem_src": away_emblem_src,
        "home_color": home_color,
        "away_color": away_color,
        "fan_side": fan_side,
    }

    return {
        "home_team": match["home_team"],
        "away_team": match["away_team"],
        "home_score": match["home_score"],
        "away_score": match["away_score"],
        "home_short": home_short,
        "away_short": away_short,
        "home_badge_letter": home_badge_letter,
        "away_badge_letter": away_badge_letter,
        "home_emblem_src": home_emblem_src,
        "away_emblem_src": away_emblem_src,
        "home_color": home_color,
        "away_color": away_color,
        "kickoff_display": _format_kickoff(match.get("kickoff_iso")),
        "section": match.get("section", ""),
        "stadium": match.get("stadium", ""),
        "attendance": match.get("attendance"),
        "score_goal_line": _build_score_goal_line(match),
        "headline": headline,
        "summary_paragraphs": article.get("summary_paragraphs", []),
        "home_comments": home_comments,
        "away_comments": away_comments,
        "home_section_title": home_section_title,
        "away_section_title": away_section_title,
        "tweets": tweets,
        "nichan_picks": nichan_picks,
        "nichan_thread_title": nichan_thread_title,
        "blog_source_name": _blog_source_name(blog_url),
        "game_id": game_id,
        "match_meta_json": json.dumps(match_meta, ensure_ascii=False),
    }


def render_from_context(context: dict, output_path: Path) -> str:
    """既に組み立て済みのcontext(dict)をテンプレートに描画してファイルに書き出す。
    (run_pipeline.py のように、ファイル経由ではなくメモリ上のデータをそのまま渡したい場合用)"""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("nagoya_shimizu_reaction.html.j2")
    html = template.render(**context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return html


def render(
    match_path: Path,
    reactions_path: Path,
    article_path: Path,
    output_path: Path,
    nichan_path: Path | None = None,
    focus_club: str | None = None,
    game_id: int | None = None,
) -> None:
    match = json.loads(match_path.read_text(encoding="utf-8"))
    reactions = json.loads(reactions_path.read_text(encoding="utf-8"))
    article = json.loads(article_path.read_text(encoding="utf-8"))
    nichan = (
        json.loads(nichan_path.read_text(encoding="utf-8"))
        if nichan_path and nichan_path.exists()
        else None
    )

    context = build_context(match, reactions, article, nichan, focus_club, game_id)
    render_from_context(context, output_path)


if __name__ == "__main__":
    import generate_article as _generate_article

    render(
        match_path=DATA_DIR / "match_result.json",
        reactions_path=DATA_DIR / "blog_reactions.json",
        article_path=DATA_DIR / "generated_article.json",
        output_path=OUTPUT_DIR / "nagoya_shimizu_reaction.html",
        nichan_path=DATA_DIR / "nichan_reactions.json",
        focus_club=_generate_article.FOCUS_CLUB,
    )
    print(f"[saved] {OUTPUT_DIR / 'nagoya_shimizu_reaction.html'}")
