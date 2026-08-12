"""
5ch(旧2ch)の実況・雑談スレッドから、試合に紐づく投稿を取得する。

5ch公式ドメインは弾かれやすいことがあるため、有志ミラードメイン
(例: kizuna.5ch.io, rio2016.5ch.io) 経由で昔ながらの
  <板URL>/subject.txt   … スレッド一覧 (Shift-JIS, "ID.dat<>タイトル (レス数)")
  <板URL>/dat/ID.dat    … スレッド本文 (Shift-JIS, 1行1レス)
形式を取得する。

.dat の1行は "<>" 区切りで5フィールド:
  名前<>メール(sage等)<>日付+ID<>本文(<br>で改行、&gt;&gt;N で安価)<>スレタイ(1行目だけ)

ドメサカブログと同様、ここで取得するのは「素材」であり、
著作権ルール(短い逐語引用は1件15語程度まで、他は要約)の適用は
generate_article.py 側のClaude呼び出しで行う想定。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class ThreadInfo:
    thread_id: str
    title: str
    res_count: int


@dataclass
class NichanPost:
    number: int
    posted_at: str  # 例: "2026/08/08(土) 19:38:12.34"
    poster_id: str | None
    text: str
    reply_to: list[int] = field(default_factory=list)  # 本文中の安価(>>N)先


@dataclass
class NichanThread:
    board_url: str
    thread_id: str
    title: str
    url: str
    posts: list[NichanPost] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "board_url": self.board_url,
            "thread_id": self.thread_id,
            "title": self.title,
            "url": self.url,
            "posts": [
                {
                    "number": p.number,
                    "posted_at": p.posted_at,
                    "poster_id": p.poster_id,
                    "text": p.text,
                    "reply_to": p.reply_to,
                }
                for p in self.posts
            ],
        }


def _fetch_bytes(url: str, timeout: int = 20) -> bytes:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _decode_5ch(raw: bytes) -> str:
    # 5ch系は伝統的にShift-JIS(実質はCP932)。UTF-8で来ることもあるため両対応。
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def find_threads(board_url: str, keyword: str, timeout: int = 20) -> list[ThreadInfo]:
    """
    <板URL>/subject.txt を取得し、タイトルに keyword を含むスレッドを新しい順に返す。
    (subject.txt は基本的に新着・勢い順に並んでいる)
    """
    raw = _fetch_bytes(f"{board_url.rstrip('/')}/subject.txt", timeout=timeout)
    text = _decode_5ch(raw)

    threads: list[ThreadInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "<>" not in line:
            continue
        dat_name, _, rest = line.partition("<>")
        thread_id = dat_name.removesuffix(".dat")
        m = re.match(r"^(.*)\s+\((\d+)\)\s*$", rest)
        if not m:
            continue
        title, res_count = m.group(1).strip(), int(m.group(2))
        if keyword in title:
            threads.append(ThreadInfo(thread_id=thread_id, title=title, res_count=res_count))
    return threads


_REPLY_PATTERN = re.compile(r"&gt;&gt;(\d+)")
_TAG_PATTERN = re.compile(r"<[^>]+>")


def _clean_body(raw_body: str) -> tuple[str, list[int]]:
    """
    .dat内の本文フィールドをクリーンなテキストに変換する。
    <br> → 改行、<a href="...">&gt;&gt;N</a> → 安価番号を reply_to に集めつつ本文からは除去、
    それ以外のHTMLタグは除去、HTMLエンティティはデコードする。
    """
    reply_to = [int(n) for n in _REPLY_PATTERN.findall(raw_body)]

    body = re.sub(r"<br\s*/?>", "\n", raw_body, flags=re.IGNORECASE)
    body = _TAG_PATTERN.sub("", body)  # <a>...</a> の残骸などを含む全タグを除去
    body = html.unescape(body)
    # 安価行 (">>1" だけの行) は情報量が無いので取り除く
    lines = [ln for ln in (l.strip() for l in body.split("\n")) if not re.fullmatch(r"(>>\d+\s*)+", ln)]
    body = "\n".join(ln for ln in lines if ln)
    return body.strip(), reply_to


def fetch_thread(board_url: str, thread_id: str, timeout: int = 20) -> NichanThread:
    raw = _fetch_bytes(f"{board_url.rstrip('/')}/dat/{thread_id}.dat", timeout=timeout)
    text = _decode_5ch(raw)

    posts: list[NichanPost] = []
    title = ""
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split("<>")
        if len(fields) < 4:
            continue
        _name, _mail, date_id, raw_body = fields[0], fields[1], fields[2], fields[3]
        if i == 1 and len(fields) >= 5:
            title = fields[4].strip()

        posted_at, _, id_part = date_id.partition(" ID:")
        poster_id = id_part.strip() or None

        body, reply_to = _clean_body(raw_body)
        if not body:
            continue  # 安価だけ・空レスなど本文が残らなかったものは除く

        posts.append(
            NichanPost(
                number=i,
                posted_at=posted_at.strip(),
                poster_id=poster_id,
                text=body,
                reply_to=reply_to,
            )
        )

    return NichanThread(
        board_url=board_url,
        thread_id=thread_id,
        title=title,
        url=f"{board_url.rstrip('/')}/test/read.cgi/{board_url.rstrip('/').rsplit('/', 1)[-1]}/{thread_id}/",
        posts=posts,
    )


# 相槌・単発煽りなど、要約対象として値の低い定型レスをふるい落とす簡易フィルタ。
# 厳密な判定はできないので「明らかにノイズ」なものだけ弾く、控えめな設計にしている。
_LOW_VALUE_PATTERNS = [
    re.compile(r"^(いちおつ|1乙|乙|otsu|www*|うぽつ)$", re.IGNORECASE),
    re.compile(r"^age$", re.IGNORECASE),
]
_MIN_LEN = 6


def is_low_value(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < _MIN_LEN:
        return True
    return any(p.match(stripped) for p in _LOW_VALUE_PATTERNS)


def get_meaningful_posts(thread: NichanThread) -> list[NichanPost]:
    return [p for p in thread.posts if not is_low_value(p.text)]


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    # 例: サッカーch(実況)板でグランパスのスレッドを探す
    board = sys.argv[1] if len(sys.argv) > 1 else "https://rio2016.5ch.io/livefoot"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "グランパス"

    print(f"[検索] {board}/subject.txt から「{keyword}」を含むスレッドを検索...")
    candidates = find_threads(board, keyword)
    if not candidates:
        print("該当スレッドが見つかりませんでした。")
        sys.exit(1)

    for c in candidates:
        print(f"  - {c.thread_id}  ({c.res_count}レス)  {c.title}")

    target = candidates[0]
    print(f"\n[取得] {target.title} ({target.thread_id}) を取得します...")
    thread = fetch_thread(board, target.thread_id)
    meaningful = get_meaningful_posts(thread)

    print(f"総レス数: {len(thread.posts)}  /  ノイズ除去後: {len(meaningful)}")
    print("-" * 60)
    for p in meaningful[:15]:
        print(f"[{p.number}] {p.posted_at}")
        print(f"  {p.text[:80].replace(chr(10), ' / ')}")
    print("-" * 60)

    out_path = Path(__file__).resolve().parent.parent / "data" / "nichan_reactions.json"
    out_path.write_text(
        json.dumps(
            {**thread.to_dict(), "posts": [
                {
                    "number": p.number,
                    "posted_at": p.posted_at,
                    "poster_id": p.poster_id,
                    "text": p.text,
                    "reply_to": p.reply_to,
                }
                for p in meaningful
            ]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[saved] {out_path}")
