"""
既存の docs/{game_id}.html (旧テンプレートで生成済みのページ) を、新テンプレートの
見た目(favicon・クラブエンブレム/クラブカラー・前後ナビ・match-metaメタデータ)に
合わせて書き換える、1回きりのメンテナンススクリプト。

Claude APIは再呼び出ししない(まとめ文・引用は既に公開済みのものをそのまま使う。
再生成すると文面が変わってしまう上にAPI課金も無駄になるため)。
その代わり、各ページのHTML文字列を正規表現で直接パッチする:
  - <style>...</style> ブロックを最新テンプレートのCSSで丸ごと置き換え
  - favicon <link> を挿入
  - ページ先頭に page-nav プレースホルダを挿入(前後リンクは run_pipeline.update_index が後で埋める)
  - </body> 直前に match-meta の <script> を挿入
  - スコアボード・セクション見出し・コメント欄の色付き円+文字バッジを、
    Jリーグ公式サイトから再取得したエンブレム画像+クラブカラーに置き換える
    (試合結果自体はJリーグ公式から無料で再取得できるので、ここだけは実際にAPIを叩き直す)

既に新テンプレートで生成されたページ(match-metaを含む)はスキップする(冪等)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import emblem
import jleague_scraper
import render_html

BASE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = BASE_DIR / "docs"
TEMPLATE_PATH = BASE_DIR / "templates" / "nagoya_shimizu_reaction.html.j2"
EMBLEM_ASSETS_DIR = DOCS_DIR / "assets" / "emblems"

FALLBACK_HOME_COLOR = "#8c1d20"
FALLBACK_AWAY_COLOR = "#e8781f"


def _game_url(game_id: int) -> str:
    return f"https://www.jleague.jp/match/j1/2026/{str(game_id)[4:]}/"


def _load_new_style_block() -> str:
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    m = re.search(r"<style>.*?</style>", template_text, re.S)
    if not m:
        raise RuntimeError("テンプレートから<style>ブロックが見つかりませんでした")
    return m.group(0)


_STYLE_RE = re.compile(r"<style>.*?</style>", re.S)
_FAVICON_RE = re.compile(r'<link rel="icon"[^>]*>')
_TITLE_TAG_RE = re.compile(r"<title>.*?</title>", re.S)
_CLUB_NAME_RE = re.compile(r'<div class="club-name">(.*?)</div>')
_SCORE_NUM_RE = re.compile(
    r'<div class="score-num">(\d+)<span class="score-dash">.*?</span>(\d+)</div>'
)
_WRAP_OPEN_RE = re.compile(r'(<div class="wrap">\s*)')
_BODY_CLOSE_RE = re.compile(r"(</body>)")

_FAVICON_TAG = (
    '<link rel="icon" href="data:image/svg+xml,'
    '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22>'
    '<text y=%22.9em%22 font-size=%2290%22>⚽</text></svg>">'
)

_PAGE_NAV_PLACEHOLDER = (
    "<!--PAGE-NAV-START-->\n"
    '  <div id="page-nav" class="page-nav">\n'
    '    <a class="page-nav-back" href="index.html">← 一覧に戻る</a>\n'
    "  </div>\n"
    "  <!--PAGE-NAV-END-->\n\n  "
)


def _badge_scoreboard_html(letter: str, emblem_src: str | None, color: str) -> str:
    if emblem_src:
        return f'<div class="badge"><img src="{emblem_src}" alt=""></div>'
    return f'<div class="badge" style="background:{color}">{letter}</div>'


def _badge_mini_html(tag: str, letter: str, emblem_src: str | None, color: str) -> str:
    if emblem_src:
        return f'<{tag} class="badge mini"><img src="{emblem_src}" alt=""></{tag}>'
    return f'<{tag} class="badge mini" style="background:{color}">{letter}</{tag}>'


def migrate_page(path: Path, new_style_block: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if 'id="match-meta"' in text:
        print(f"[skip] {path.name} は既に新テンプレート形式です")
        return False

    game_id = int(path.stem)

    club_names = _CLUB_NAME_RE.findall(text)
    score_m = _SCORE_NUM_RE.search(text)
    if len(club_names) != 2 or not score_m:
        print(f"[warn] {path.name}: 旧ページからチーム名/スコアを抽出できませんでした。スキップします。")
        return False
    old_home_team, old_away_team = club_names
    old_home_score, old_away_score = int(score_m.group(1)), int(score_m.group(2))

    # 試合結果はJリーグ公式サイトから無料で再取得できる(API課金なし)。
    # ここでクラブエンブレム/クラブカラー/正確なkickoff_isoを取得する。
    match_result = jleague_scraper.get_match_result(_game_url(game_id))
    match_dict = match_result.to_dict()

    if match_dict["home_team"] != old_home_team or match_dict["away_team"] != old_away_team:
        print(
            f"[warn] {path.name}: チーム名が一致しません "
            f"(旧:{old_home_team}/{old_away_team} 新:{match_dict['home_team']}/{match_dict['away_team']})。"
            "安全のためスキップします。"
        )
        return False
    if match_dict["home_score"] != old_home_score or match_dict["away_score"] != old_away_score:
        print(f"[warn] {path.name}: スコアが一致しません。安全のためスキップします。")
        return False

    home_visual = match_dict.get("home_visual")
    away_visual = match_dict.get("away_visual")
    home_emblem_src = emblem.get_emblem_relative_path(home_visual, EMBLEM_ASSETS_DIR)
    away_emblem_src = emblem.get_emblem_relative_path(away_visual, EMBLEM_ASSETS_DIR)
    home_color = (home_visual or {}).get("primary_color") or FALLBACK_HOME_COLOR
    away_color = (away_visual or {}).get("primary_color") or FALLBACK_AWAY_COLOR
    home_letter = old_home_team[0]
    away_letter = old_away_team[0]

    # --- 1. <style>ブロックを最新CSSに差し替え ---------------------------------
    text = _STYLE_RE.sub(lambda _m: new_style_block, text, count=1)

    # --- 2. favicon挿入 ---------------------------------------------------------
    if not _FAVICON_RE.search(text):
        text = _TITLE_TAG_RE.sub(lambda m: m.group(0) + "\n" + _FAVICON_TAG, text, count=1)

    # --- 3. page-nav プレースホルダ挿入(前後リンクはupdate_indexが後で埋める) -----
    if "PAGE-NAV-START" not in text:
        text = _WRAP_OPEN_RE.sub(lambda m: m.group(1) + "\n  " + _PAGE_NAV_PLACEHOLDER, text, count=1)

    # --- 4. match-meta 埋め込み ---------------------------------------------------
    match_meta = {
        "game_id": game_id,
        "section": match_dict.get("section", ""),
        "kickoff_iso": match_dict.get("kickoff_iso"),
        "home_team": match_dict["home_team"],
        "away_team": match_dict["away_team"],
        "home_score": match_dict["home_score"],
        "away_score": match_dict["away_score"],
        "home_short": render_html._team_short(match_dict["home_team"]),
        "away_short": render_html._team_short(match_dict["away_team"]),
        "home_emblem_src": home_emblem_src,
        "away_emblem_src": away_emblem_src,
        "home_color": home_color,
        "away_color": away_color,
        "fan_side": None,
    }
    meta_tag = f'<script type="application/json" id="match-meta">{json.dumps(match_meta, ensure_ascii=False)}</script>\n'
    text = _BODY_CLOSE_RE.sub(lambda m: meta_tag + m.group(1), text, count=1)

    # --- 5. スコアボードの色付き円バッジ → エンブレム/クラブカラー -----------------
    text = text.replace(
        f'<div class="badge nagoya">{home_letter}</div>',
        _badge_scoreboard_html(home_letter, home_emblem_src, home_color),
    )
    text = text.replace(
        f'<div class="badge shimizu">{away_letter}</div>',
        _badge_scoreboard_html(away_letter, away_emblem_src, away_color),
    )

    # --- 6. セクション見出し・コメント欄のミニバッジ → 同上 ------------------------
    for tag in ("span", "div"):
        text = text.replace(
            f'<{tag} class="badge mini nagoya">{home_letter}</{tag}>',
            _badge_mini_html(tag, home_letter, home_emblem_src, home_color),
        )
        text = text.replace(
            f'<{tag} class="badge mini shimizu">{away_letter}</{tag}>',
            _badge_mini_html(tag, away_letter, away_emblem_src, away_color),
        )

    path.write_text(text, encoding="utf-8")
    print(f"[ok] {path.name} を移行しました")
    return True


def main() -> None:
    new_style_block = _load_new_style_block()
    count = 0
    for f in sorted(DOCS_DIR.glob("*.html")):
        if f.name == "index.html":
            continue
        try:
            if migrate_page(f, new_style_block):
                count += 1
        except Exception:
            print(f"[error] {f.name} の移行に失敗しました")
            traceback.print_exc()
    print(f"[done] {count} 件のページを移行しました")


if __name__ == "__main__":
    main()
