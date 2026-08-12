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
import sys
import traceback
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
NICHAN_KEYWORD = "グランパス"


def log(msg: str) -> None:
    print(f"[pipeline] {msg}", flush=True)


def run() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)

    # --- 1. 対象試合の自動検出 -------------------------------------------------
    match_info = discover_match.find_latest_completed_match()
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
        article_ref = discover_match.find_blog_article(match_date=match_date)
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
        threads = nichan_scraper.find_threads(NICHAN_BOARD, NICHAN_KEYWORD)
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
    gen_article, verification = generate_article.generate_article(match_dict, reactions_dict, nichan_dict)
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
    context = render_html.build_context(match_dict, reactions_dict, generated_dict, nichan_dict)
    render_html.render_from_context(context, output_path)
    log(f"HTML生成OK: {output_path}")

    # --- 7. 一覧ページ(docs/index.html)更新 --------------------------------------
    update_index()
    log("index.html 更新OK")

    return 0


def update_index() -> None:
    """docs/ 配下の各試合ページを走査し、新しい順に並べた一覧ページを作る。"""
    pages = []
    for f in sorted(DOCS_DIR.glob("*.html"), reverse=True):
        if f.name == "index.html":
            continue
        text = f.read_text(encoding="utf-8")
        title_start = text.find("<title>")
        title_end = text.find("</title>")
        title = text[title_start + 7 : title_end] if title_start != -1 else f.stem
        pages.append((f.name, title))

    items = "\n".join(
        f'    <li><a href="{name}">{title}</a></li>' for name, title in pages
    )
    html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>名古屋グランパス 反応まとめ一覧</title>
<style>
  body{{font-family:"Meiryo UI","Meiryo","メイリオ",sans-serif;background:#f6f2ea;color:#241f19;max-width:700px;margin:40px auto;padding:0 20px;}}
  h1{{font-size:22px;}}
  ul{{list-style:none;padding:0;}}
  li{{padding:12px 0;border-bottom:1px solid #ddd3bf;}}
  a{{color:#8c1d20;text-decoration:none;}}
  a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
<h1>名古屋グランパス 反応まとめ一覧</h1>
<ul>
{items}
</ul>
</body>
</html>
"""
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(run())
