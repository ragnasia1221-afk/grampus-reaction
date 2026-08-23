"""
1回きりのメンテナンススクリプト(2026-08-23、方針修正)。

前回のmigrate_onechar.pyでは、セクション見出し・home/awayコメントの「who」バッジを
エンブレム画像→クラブ一文字表記(色つき円)に置き換えたが、ユーザーからのフィードバックで
「既存のエンブレム表示はそのままでよく、一文字表記は追加のルールとして使いたかった」
と判明した。そのため方針を修正する:

1. セクション見出し・home/awayコメントの「who」バッジ: エンブレム画像表示に戻す
   (migrate_onechar.pyが行った変更を打ち消す)。
2. 「他クラブ」コメントのバッジ: 一文字表記(render_html.CLUB_ONE_CHAR)からクラブを
   逆引きできた場合は、そのクラブの実際のクラブカラーを円の背景色に使う
   (render_html._club_color_reference()が全試合ページのmatch-metaを集約して作る参照表を使う)。
   逆引きできない・匿名(👤)の場合は従来通りグレー(var(--other))のまま。

外部への再スクレイピングやClaude API呼び出しは一切不要(すべてmatch-metaと
既存ページ間の情報だけで完結する)。
"""

from __future__ import annotations

import json
import re
import sys
import traceback
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_html

BASE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = BASE_DIR / "docs"

_MATCH_META_RE = re.compile(
    r'<script type="application/json" id="match-meta">(.*?)</script>', re.S
)

# home/awayの色つき円バッジ(migrate_onechar.py由来)を検出して、対応するエンブレム画像に戻す。
_HOME_AWAY_BADGE_RE = re.compile(
    r'<(span|div) class="badge mini" style="background:(#[0-9A-Fa-f]{6})">([^<]*)</\1>'
)

# 「他クラブ」コメントのバッジ(新旧2種類の書式に対応)。
_OTHER_BADGE_RE = re.compile(
    r'<div class="who">\s*'
    r'(?:<div class="badge mini other">([^<]*)</div>'
    r'|<div class="badge mini" style="background:(#[0-9A-Fa-f]{6}|var\(--other\))">([^<]*)</div>)'
    r'\s*<span class="tag">他クラブ</span></div>'
)


def _restore_home_away_crests(text: str, meta: dict) -> tuple[str, int]:
    home_team, away_team = meta["home_team"], meta["away_team"]
    home_emblem_src = meta.get("home_emblem_src")
    away_emblem_src = meta.get("away_emblem_src")
    home_color = meta.get("home_color") or render_html.FALLBACK_HOME_COLOR
    away_color = meta.get("away_color") or render_html.FALLBACK_AWAY_COLOR
    home_char = render_html._club_one_char(home_team)
    away_char = render_html._club_one_char(away_team)

    replaced = 0

    def _replace(m: re.Match) -> str:
        nonlocal replaced
        tag, color, letter = m.groups()
        if color == home_color and letter == home_char and home_emblem_src:
            replaced += 1
            return f'<{tag} class="badge mini"><img src="{home_emblem_src}" alt=""></{tag}>'
        if color == away_color and letter == away_char and away_emblem_src:
            replaced += 1
            return f'<{tag} class="badge mini"><img src="{away_emblem_src}" alt=""></{tag}>'
        return m.group(0)  # home/awayのどちらとも一致しない(=他クラブの偶然の一致等) → 触らない

    new_text = _HOME_AWAY_BADGE_RE.sub(_replace, text)
    return new_text, replaced


def _recolor_other_badges(text: str) -> tuple[str, int]:
    color_ref = render_html._club_color_reference()
    fixed = 0

    def _replace(m: re.Match) -> str:
        nonlocal fixed
        letter = m.group(1) if m.group(1) is not None else m.group(3)
        letter = (letter or "").strip()

        if letter == render_html.ANONYMOUS_BADGE_ICON or not letter:
            new_style = "var(--other)"
        else:
            other_club = render_html._ONE_CHAR_TO_CLUB.get(letter)
            color = (
                color_ref.get(unicodedata.normalize("NFKC", other_club))
                if other_club
                else None
            )
            new_style = color or "var(--other)"

        fixed += 1
        return (
            '<div class="who">\n        '
            f'<div class="badge mini" style="background:{new_style}">{letter}</div>\n        '
            '<span class="tag">他クラブ</span></div>'
        )

    new_text = _OTHER_BADGE_RE.sub(_replace, text)
    return new_text, fixed


def migrate_page(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    m = _MATCH_META_RE.search(text)
    if not m:
        print(f"[skip] {path.name}: match-meta が見つかりません")
        return False
    meta = json.loads(m.group(1))

    new_text, n_crest = _restore_home_away_crests(text, meta)
    new_text, n_other = _recolor_other_badges(new_text)

    changed = n_crest > 0 or n_other > 0
    if changed:
        path.write_text(new_text, encoding="utf-8")
        print(f"[ok] {path.name}: エンブレム復元 {n_crest} 件 / 他クラブ再配色 {n_other} 件")
    else:
        print(f"[skip] {path.name}: 置換対象なし")
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
