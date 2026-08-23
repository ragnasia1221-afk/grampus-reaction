"""
既存の docs/{game_id}.html のセクション見出し・コメント欄バッジを、
クラブの一文字表記(render_html.CLUB_ONE_CHAR)を使った色つき円バッジに置き換える
1回きりのメンテナンススクリプト(2026-08-23)。

対象:
1. home/away チームのバッジ(セクション見出し + 各コメントの「who」バッジ)。
   各ページに埋め込まれた match-meta から home_team/away_team/color を読み取れるので、
   外部への再スクレイピングやClaude API呼び出しは一切不要(スコアボードのエンブレム画像は
   意図的にそのまま)。
2. 「他クラブ」コメントのバッジ文字。旧パイプラインでは投稿者が自由入力した
   team_tag(例:「名無しさん」)の先頭一文字をそのまま使っていたため、
   匿名投稿が「名」という紛らわしい文字になっていた(render_html._is_anonymous_name参照)。
   この情報は元のページには残っていないため、ドメサカブログの元記事を無料で再取得し
   (Claude APIは呼ばない)、コメント番号でひも付けて正しい文字/人物アイコンに直す。
   ブログ記事が見つからない・コメントが削除されている等の理由で再取得できない場合は
   その回だけ諦めて元のバッジのまま残す(安全側に倒す)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover_match
import domesoccer_scraper
import render_html

BASE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = BASE_DIR / "docs"

_MATCH_META_RE = re.compile(
    r'<script type="application/json" id="match-meta">(.*?)</script>', re.S
)

# 「他クラブ」コメントのバッジ: <div class="who"><div class="badge mini ...">X</div>
# <span class="tag">他クラブ</span></div> ... <div class="meta">コメント{N}</div>
# という並びから、バッジ文字Xとコメント番号Nを両方とも取り出す。
# テンプレートの版によって2種類の書式が混在しているため両方に対応する:
#  - 旧: <div class="badge mini other">X</div> (改行無し、最初期のテンプレート)
#  - 新: <div class="badge mini" style="background:var(--other)">X</div> (前回セッションの
#        エンブレム対応テンプレート、改行・インデント入り)
_OTHER_BADGE_RE = re.compile(
    r'(<div class="who">\s*'
    r'(?:<div class="badge mini other">'
    r'|<div class="badge mini" style="background:var\(--other\)">))'
    r'([^<]*)'
    r'(</div>\s*<span class="tag">他クラブ</span></div>\s*'
    r'<div class="body">\s*<div class="meta">コメント(\d+)</div>)',
    re.S,
)


def _mini_crest_pattern(tag: str, emblem_src: str) -> re.Pattern:
    escaped = re.escape(emblem_src)
    return re.compile(
        rf'<{tag} class="badge mini"><img src="{escaped}" alt="[^"]*"></{tag}>'
    )


def _fetch_number_to_team_tag(meta: dict) -> dict[int, str]:
    """ブログ記事を無料で再取得し、コメント番号→team_tag の対応を作る。
    取得できない場合は空辞書を返す(呼び出し側は「他クラブ」バッジをそのまま残す)。"""
    kickoff_iso = meta.get("kickoff_iso")
    if not kickoff_iso:
        return {}
    match_date = dt.datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")).date()
    keyword = f"{meta['home_team']} {meta['away_team']}"
    article = discover_match.find_blog_article(keyword=keyword, match_date=match_date)
    if article is None:
        return {}
    reactions = domesoccer_scraper.get_blog_reactions(
        article.url, home_team=meta["home_team"], away_team=meta["away_team"]
    )
    return {c.number: c.team_tag for c in reactions.comments if c.number is not None}


def _fix_other_badges(text: str, meta: dict) -> tuple[str, int]:
    number_to_tag = _fetch_number_to_team_tag(meta)
    if not number_to_tag:
        return text, 0

    fixed = 0

    def _replace(m: re.Match) -> str:
        nonlocal fixed
        prefix, _old_letter, suffix, number_str = m.groups()
        team_tag = number_to_tag.get(int(number_str))
        if team_tag is None:
            return m.group(0)  # 再取得したコメント一覧に見つからない → 元のまま
        team_tag = team_tag.strip()
        if render_html._is_anonymous_name(team_tag):
            new_letter = render_html.ANONYMOUS_BADGE_ICON
        else:
            new_letter = team_tag[0] if team_tag else "他"
        fixed += 1
        return f"{prefix}{new_letter}{suffix}"

    new_text = _OTHER_BADGE_RE.sub(_replace, text)
    return new_text, fixed


def migrate_page(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    m = _MATCH_META_RE.search(text)
    if not m:
        print(f"[skip] {path.name}: match-meta が見つかりません")
        return False

    meta = json.loads(m.group(1))
    home_team, away_team = meta["home_team"], meta["away_team"]
    home_emblem_src = meta.get("home_emblem_src")
    away_emblem_src = meta.get("away_emblem_src")
    home_color = meta.get("home_color") or render_html.FALLBACK_HOME_COLOR
    away_color = meta.get("away_color") or render_html.FALLBACK_AWAY_COLOR
    home_char = render_html._club_one_char(home_team)
    away_char = render_html._club_one_char(away_team)

    new_text = text
    changed = False
    for tag in ("span", "div"):
        if home_emblem_src:
            pattern = _mini_crest_pattern(tag, home_emblem_src)
            replacement = f'<{tag} class="badge mini" style="background:{home_color}">{home_char}</{tag}>'
            new_text, n = pattern.subn(replacement, new_text)
            changed = changed or n > 0
        if away_emblem_src:
            pattern = _mini_crest_pattern(tag, away_emblem_src)
            replacement = f'<{tag} class="badge mini" style="background:{away_color}">{away_char}</{tag}>'
            new_text, n = pattern.subn(replacement, new_text)
            changed = changed or n > 0

    try:
        new_text, n_other_fixed = _fix_other_badges(new_text, meta)
    except Exception:
        print(f"[warn] {path.name}: 「他クラブ」バッジの再取得に失敗しました。このページはスキップします。")
        traceback.print_exc()
        n_other_fixed = 0
    changed = changed or n_other_fixed > 0

    if changed:
        path.write_text(new_text, encoding="utf-8")
        print(
            f"[ok] {path.name}: {home_team}→{home_char} / {away_team}→{away_char}"
            f" (他クラブバッジ修正 {n_other_fixed} 件)"
        )
    else:
        print(f"[skip] {path.name}: 置換対象なし(既に移行済みの可能性)")
    return changed


def main() -> None:
    count = 0
    for f in sorted(DOCS_DIR.glob("*.html")):
        if f.name == "index.html":
            continue
        try:
            if migrate_page(f):
                count += 1
        except Exception:
            print(f"[error] {f.name} の移行に失敗しました")
            traceback.print_exc()
    print(f"[done] {count} 件のページを移行しました")


if __name__ == "__main__":
    main()
