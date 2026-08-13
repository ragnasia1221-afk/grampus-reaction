"""
Jリーグ公式サイトのクラブエンブレム画像(スプライトシート形式)から、
個別クラブのエンブレムだけを切り出してキャッシュするユーティリティ。

jleague_scraper.py が試合結果ページから取得する各チームの "icon" 情報
(スプライト画像URL + 切り出し位置 x,y + 1マスのサイズ)を使って、
1枚の大きなスプライト画像から該当クラブの正方形部分だけをPillowで切り出す。

切り出し結果は docs/assets/emblems/ に保存し、生成したHTMLからは
相対パスで参照する(Jリーグ公式サイトへの直リンクではなく、自サイトの
静的ファイルとして配信する)。同じクラブは同じファイル名になるよう
キャッシュキー(スプライトURL+x+y のハッシュ)でファイル名を決めているため、
複数試合で同じクラブが登場しても再ダウンロード・再切り出しは1回で済む。
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

JLEAGUE_BASE = "https://www.jleague.jp"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_sprite_cache: dict[str, Image.Image] = {}


def _get_sprite_sheet(sprite_url: str, timeout: int = 20) -> Image.Image:
    if sprite_url not in _sprite_cache:
        full_url = sprite_url if sprite_url.startswith("http") else f"{JLEAGUE_BASE}{sprite_url}"
        resp = requests.get(full_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        _sprite_cache[sprite_url] = Image.open(BytesIO(resp.content)).convert("RGBA")
    return _sprite_cache[sprite_url]


def get_emblem_relative_path(visual: dict | None, assets_dir: Path) -> str | None:
    """
    visual: jleague_scraper.TeamVisual を dict 化したもの
            ({"emblem_sprite_url": ..., "emblem_x": ..., "emblem_y": ..., "emblem_cell_size": ...})
    assets_dir: 切り出し画像の保存先ディレクトリ (例: docs/assets/emblems)

    戻り値: docs/ を基準とした相対パス (例: "assets/emblems/ab12cd34ef56.png")。
            取得・切り出しに失敗した場合は None (呼び出し側は色つき円+文字表示にフォールバックする)。
    """
    if not visual:
        return None
    sprite_url = visual.get("emblem_sprite_url")
    x, y, size = visual.get("emblem_x"), visual.get("emblem_y"), visual.get("emblem_cell_size")
    if not sprite_url or x is None or y is None or not size:
        return None

    cache_key = f"{sprite_url}:{x}:{y}:{size}"
    filename = hashlib.md5(cache_key.encode("utf-8")).hexdigest()[:16] + ".png"
    assets_dir.mkdir(parents=True, exist_ok=True)
    out_path = assets_dir / filename

    if not out_path.exists():
        try:
            sheet = _get_sprite_sheet(sprite_url)
            left, top = -x, -y  # x,yはbackground-position相当の負値オフセット
            crop = sheet.crop((left, top, left + size, top + size))
            crop.save(out_path)
        except Exception:
            return None

    return f"assets/emblems/{filename}"
