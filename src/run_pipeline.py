"""
パイプライン全体(試合自動検出 → 3ソーススクレイピング → Claude生成 → HTML描画 → 一覧更新)
を1回分実行するオーケストレーター。GitHub Actionsの日次実行から呼ばれる想定。

設計方針:
- Jリーグ公式の試合結果は必須(取れなければ失敗として終了)。
- ドメサカブログ・5chは「取れれば使う」任意ソース。取得に失敗しても
  パイプライン全体は止めず、その分のセクションが無いページを生成する
  (ミラードメインの不安定さなど、外部要因で落ちうるため)。
- 同じ試合を重複生成しないよう、出力ファイル(docs/{game_id}.html)が既にあれば
  何もせず正常終了する(cronで毎日叩いても安全)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import traceback
from datetime import timedelta, timezone
from pathlib import Path

import discover_match
import domesoccer_scraper
import generate_article
import jleague_scraper
import nichan_scraper
import render_html

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"

NICHAN_BOARD = "https://rio2016.5ch.io/livefoot"


def _nichan_keyword_for_team(team_name: str) -> str:
    """
    5ch実況スレ探索用のキーワードを決める。
    スレッドの命名規則はクラブごとにバラバラ(「実況板にも集え！〇〇ファン」
    「〇〇実況2026☆N」「◆◇◆　〇〇　実況　◆◇◆」等)だが、
    クラブの通称(ニックネーム)で検索すればどのパターンでも部分一致でヒットする
    (domesoccer_scraper.TEAM_NICKNAMES の先頭エイリアスを流用)。
    """
    aliases = domesoccer_scraper.get_team_aliases(team_name)
    return aliases[0] if aliases else team_name


def log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)


def run(team_slug: str = "nagoya") -> int:
    """
    team_slug: jleague.jpのクラブURL識別子。既定はグランパス(nagoya)で、
    GitHub Actionsからの日次実行はこの既定値のまま呼ばれる想定。
    他クラブの試合で動作確認したい場合は明示的に指定する
    (例: run_pipeline.py kashima)。
    """
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)

    # --- 1. 対象試合の自動検出 -------------------------------------------------
    match_info = discover_match.find_latest_completed_match(team_slug=team_slug)
    log(f"最新消化試合: {match_info.game_date} vs {match_info.opponent} (gameId={match_info.game_id})")

    output_path = DOCS_DIR / f"{match_info.game_id}.html"
    if output_path.exists():
        log(f"{output_path.name} は既に生成済みです。何もせず終了します。")
        return 0

    # --- 2. Jリーグ公式: 試合結果 (必須) -----------------------------------------
    match_result = jleague_scraper.get_match_result(match_info.url)
    match_dict = match_result.to_dict()
    (DATA_DIR / "match_result.json").write_text(
        json.dumps(match_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"試合結果取得OK: {match_result.summary_line()}")

    y, m, d = match_info.game_date.split("/")
    match_date = dt.date(2000 + int(y), int(m), int(d))

    # --- 3. ドメサカブログ (任意) ------------------------------------------------
    reactions_dict: dict = {"url": "", "comments": [], "tweets": []}
    try:
        # 片方のチーム名だけで検索すると、そのチームに言及した無関係な最新記事
        # (移籍情報・ACL関連など)がヒットして日付フィルタで弾かれることがあるため、
        # 両チーム名を組み合わせて検索することで対象記事に確実に絞り込む。
        blog_keyword = f"{match_result.home_team} {match_result.away_team}"
        article_ref = discover_match.find_blog_article(keyword=blog_keyword, match_date=match_date)
        if article_ref is None:
            log("[警告] ドメサカブログの対応記事が見つかりませんでした。このソースなしで続行します。")
        else:
            reactions = domesoccer_scraper.get_blog_reactions(
                article_ref.url,
                home_team=match_result.home_team,
                away_team=match_result.away_team,
            )
            reactions_dict = reactions.to_dict()
            log(f"ブログ反応取得OK: コメント{len(reactions.comments)}件 / ツイート{len(reactions.tweets)}件")
    except Exception:
        log("[警告] ドメサカブログの取得に失敗しました。このソースなしで続行します。")
        traceback.print_exc()
    (DATA_DIR / "blog_reactions.json").write_text(
        json.dumps(reactions_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # --- 4. 5ch実況スレッド (任意) -----------------------------------------------
    nichan_dict: dict | None = None
    try:
        # FOCUS_CLUBが関与する試合ならその実況スレを、無関係な試合(中立視点)ならhome側の実況スレを探す
        if generate_article.FOCUS_CLUB in (match_result.home_team, match_result.away_team):
            nichan_team = generate_article.FOCUS_CLUB
        else:
            nichan_team = match_result.home_team
        opponent_team = (
            match_result.away_team if nichan_team == match_result.home_team else match_result.home_team
        )
        opponent_aliases = domesoccer_scraper.get_team_aliases(opponent_team) or [opponent_team]

        nichan_keyword = _nichan_keyword_for_team(nichan_team)
        candidates = nichan_scraper.find_threads(NICHAN_BOARD, nichan_keyword)
        # "地名"系のキーワード(例:「名古屋」)だと対戦相手側が立てた「vs名古屋」実況スレも
        # ヒットしてしまうことがあるため、相手チームの通称を含むタイトルは除外する
        threads = [
            t for t in candidates if not any(alias in t.title for alias in opponent_aliases)
        ]
        if not threads:
            log("[警告] 5chの対応スレッドが見つかりませんでした。このソースなしで続行します。")
        else:
            thread = nichan_scraper.fetch_thread(NICHAN_BOARD, threads[0].thread_id)
            meaningful = nichan_scraper.get_meaningful_posts(thread)
            nichan_dict = {
                **thread.to_dict(),
                "posts": [
                    {
                        "number": p.number,
                        "posted_at": p.posted_at,
                        "poster_id": p.poster_id,
                        "text": p.text,
                        "reply_to": p.reply_to,
                    }
                    for p in meaningful
                ],
            }
            log(f"5ch取得OK: {thread.title} / {len(meaningful)}件(ノイズ除去後)")
    except Exception:
        log("[警告] 5chの取得に失敗しました。このソースなしで続行します。")
        traceback.print_exc()
    if nichan_dict is not None:
        (DATA_DIR / "nichan_reactions.json").write_text(
            json.dumps(nichan_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --- 5. Claude API: まとめ・引用選定 (必須) ----------------------------------
    gen_article, verification = generate_article.generate_article(
        match_dict, reactions_dict, nichan_dict, focus_club=generate_article.FOCUS_CLUB
    )
    generated_dict = {
        "summary_paragraphs": gen_article.summary_paragraphs,
        "board_picks": [p.model_dump() for p in verification.ok_board_picks],
        "tweet_picks": [p.model_dump() for p in verification.ok_tweet_picks],
        "nichan_picks": [p.model_dump() for p in verification.ok_nichan_picks],
    }
    (DATA_DIR / "generated_article.json").write_text(
        json.dumps(generated_dict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(
        f"記事生成OK: 掲示板{len(verification.ok_board_picks)}件 / "
        f"X{len(verification.ok_tweet_picks)}件 / 5ch{len(verification.ok_nichan_picks)}件"
    )
    for w in verification.warnings:
        log(f"[検証警告] {w}")

    # --- 6. HTML描画 -------------------------------------------------------------
    context = render_html.build_context(
        match_dict,
        reactions_dict,
        generated_dict,
        nichan_dict,
        focus_club=generate_article.FOCUS_CLUB,
        game_id=match_info.game_id,
    )
    render_html.render_from_context(context, output_path)
    log(f"HTML生成OK: {output_path}")

    # --- 7. 一覧ページ(docs/index.html)更新 --------------------------------------
    update_index()
    log("index.html 更新OK")

    return 0


_MATCH_META_RE = re.compile(
    r'<script type="application/json" id="match-meta">(.*?)</script>', re.S
)
_PAGE_NAV_RE = re.compile(r"<!--PAGE-NAV-START-->.*?<!--PAGE-NAV-END-->", re.S)

JST = timezone(timedelta(hours=9))
_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _extract_match_meta(html_text: str) -> dict | None:
    """ページ末尾に埋め込まれた <script id="match-meta"> からJSONを取り出す。
    (ファイル名やtitleタグの文字列パースに頼らない、自己記述的な設計)"""
    m = _MATCH_META_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _format_short_date(kickoff_iso: str | None) -> str:
    if not kickoff_iso:
        return ""
    dt_jst = dt.datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")).astimezone(JST)
    return f"{dt_jst.month}/{dt_jst.day}({_WEEKDAY_JA[dt_jst.weekday()]})"


def _mini_badge_html(short_name: str, emblem_src: str | None, color: str | None) -> str:
    if emblem_src:
        return f'<span class="mb"><img src="{emblem_src}" alt=""></span>'
    letter = short_name[0] if short_name else "?"
    bg = color or "#4a5a63"
    return f'<span class="mb" style="background:{bg}">{letter}</span>'


def _nav_block_html(prev_meta: dict | None, next_meta: dict | None) -> str:
    def _link(meta: dict | None, arrow: str) -> str:
        if meta is None:
            return '<span class="page-nav-empty"></span>'
        label = f"{meta['home_short']} {meta['home_score']}-{meta['away_score']} {meta['away_short']}"
        cls = "page-nav-prev" if arrow == "←" else "page-nav-next"
        return f'<a class="{cls}" href="{meta["game_id"]}.html">{arrow} {label}</a>'

    return (
        "<!--PAGE-NAV-START-->\n"
        '  <div id="page-nav" class="page-nav">\n'
        '    <a class="page-nav-back" href="index.html">← 一覧に戻る</a>\n'
        f'    <div class="page-nav-adjacent">{_link(prev_meta, "←")}{_link(next_meta, "→")}</div>\n'
        "  </div>\n"
        "  <!--PAGE-NAV-END-->"
    )


def update_index() -> None:
    """docs/ 配下の各試合ページに埋め込まれた match-meta を集約し、
    (1) 節・日付ごとに整理した一覧ページ(docs/index.html)
    (2) 各ページの「前後の試合」ナビゲーション(page-nav ブロック)
    を再構築する。何度実行しても同じ入力からは同じ結果になる(冪等)。"""
    entries: list[tuple[Path, dict, str]] = []
    for f in DOCS_DIR.glob("*.html"):
        if f.name == "index.html":
            continue
        text = f.read_text(encoding="utf-8")
        meta = _extract_match_meta(text)
        if meta is None or meta.get("game_id") is None:
            continue  # 旧バージョンで生成された(match-metaが無い)ページはナビ対象外
        entries.append((f, meta, text))

    # 開催日時(無ければgame_id)の昇順 → これが前後ナビの基準になる
    entries.sort(key=lambda e: (e[1].get("kickoff_iso") or "", e[1]["game_id"]))

    for i, (f, _meta, text) in enumerate(entries):
        prev_meta = entries[i - 1][1] if i > 0 else None
        next_meta = entries[i + 1][1] if i < len(entries) - 1 else None
        new_text = _PAGE_NAV_RE.sub(_nav_block_html(prev_meta, next_meta), text, count=1)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")

    # 一覧ページ: 節ごとにグループ化(新しい節が上)、節内は開催日時の昇順
    by_section: dict[str, list[dict]] = {}
    for _, meta, _text in entries:
        by_section.setdefault(meta.get("section") or "節不明", []).append(meta)

    def _section_number(name: str) -> int:
        m = re.search(r"\d+", name)
        return int(m.group()) if m else 0

    section_blocks = []
    for section_name in sorted(by_section.keys(), key=_section_number, reverse=True):
        metas = sorted(by_section[section_name], key=lambda m: m.get("kickoff_iso") or "")
        rows = []
        for meta in metas:
            home_win = meta["home_score"] > meta["away_score"]
            away_win = meta["away_score"] > meta["home_score"]
            home_cls = "win" if home_win else ("lose" if away_win else "")
            away_cls = "win" if away_win else ("lose" if home_win else "")
            home_badge = _mini_badge_html(meta["home_short"], meta.get("home_emblem_src"), meta.get("home_color"))
            away_badge = _mini_badge_html(meta["away_short"], meta.get("away_emblem_src"), meta.get("away_color"))
            rows.append(
                f'    <li><a href="{meta["game_id"]}.html">'
                f'<span class="d">{_format_short_date(meta.get("kickoff_iso"))}</span>'
                f'<span class="card">{home_badge}<span class="team {home_cls}">{meta["home_short"]}</span>'
                f'<span class="score">{meta["home_score"]}-{meta["away_score"]}</span>'
                f'<span class="team {away_cls}">{meta["away_short"]}</span>{away_badge}</span>'
                f"</a></li>"
            )
        section_blocks.append(
            f'  <section>\n    <h2>{section_name}</h2>\n    <ul>\n' + "\n".join(rows) + "\n    </ul>\n  </section>"
        )

    body = "\n".join(section_blocks) if section_blocks else "  <p>まだ記事がありません。</p>"

    html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J1 反応まとめ一覧</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⚽</text></svg>">
<style>
  :root{{--paper:#f6f2ea;--ink:#241f19;--ink-soft:#6b6153;--line:#ddd3bf;--accent:#8c1d20;}}
  *{{box-sizing:border-box;}}
  body{{font-family:"Meiryo UI","Meiryo","メイリオ","Hiragino Kaku Gothic ProN","Yu Gothic UI",sans-serif;background:var(--paper);color:var(--ink);max-width:720px;margin:0 auto;padding:40px 20px 80px;}}
  h1{{font-size:22px;margin:0 0 8px;}}
  .lead{{font-size:12.5px;color:var(--ink-soft);margin:0 0 32px;}}
  section{{margin-bottom:32px;}}
  h2{{font-size:16px;border-left:4px solid var(--accent);padding-left:10px;margin:0 0 10px;}}
  ul{{list-style:none;padding:0;margin:0;}}
  li{{border-bottom:1px solid var(--line);}}
  li a{{display:flex;align-items:center;gap:14px;padding:10px 4px;text-decoration:none;color:var(--ink);}}
  li a:hover{{background:rgba(140,29,32,.06);}}
  .d{{flex-shrink:0;width:66px;font-size:11.5px;color:var(--ink-soft);}}
  .card{{flex:1;display:flex;align-items:center;gap:8px;font-size:14px;}}
  .mb{{flex-shrink:0;width:22px;height:22px;border-radius:50%;overflow:hidden;display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;font-weight:700;box-shadow:inset 0 0 0 1px rgba(0,0,0,.1);}}
  .mb img{{width:100%;height:100%;object-fit:cover;}}
  .team{{flex:1;min-width:0;}}
  .team.win{{font-weight:700;}}
  .team.lose{{color:var(--ink-soft);}}
  .team:last-child{{text-align:right;}}
  .score{{flex-shrink:0;font-weight:700;font-variant-numeric:tabular-nums;}}
</style>
</head>
<body>
<h1>J1 反応まとめ一覧</h1>
<p class="lead">Jリーグ公式サイト・掲示板・5chの反応をもとにClaude APIが自動生成しています。</p>
{body}
</body>
</html>
"""
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "nagoya"
    sys.exit(run(team_slug=slug))
