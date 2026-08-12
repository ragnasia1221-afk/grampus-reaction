"""
Jリーグ公式サイト (jleague.jp) の試合結果ページから、
スコア・得点者・警告(イエロー)/退場(レッド)カード情報を取得する。

jleague.jp は Next.js (App Router) 製のSPAで、ブラウザで見るとJSでレンダリングされるが、
生HTML自体には Next.js の RSC (React Server Components) ストリーミング形式で
試合データがまるごと埋め込まれている。
  <script>self.__next_f.push([1,"<chunkId>:<JSONっぽい文字列>"])</script>
という <script> タグが何百個も並んでいて、その中の1つに試合ヘッダー
(ホーム/アウェイのスコア・得点者一覧)が、複数個に警告・退場カードの
プレーバイプレー情報が入っている。

このモジュールはそれらの <script> チャンクを取り出してJSONとしてパースし、
必要な情報だけを抜き出す。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# self.__next_f.push([1,"...JS文字列リテラル..."]) を1個ずつ取り出す正規表現。
# JS文字列リテラルの内部では " は必ず \" にエスケープされているので、
# 非貪欲マッチ (.*?) で最初に現れるエスケープなしの " を終端とみなして良い。
# (貪欲マッチにすると、HTML全体を通して最後の "]) まで1個の巨大マッチになってしまう)
_NEXT_F_PATTERN = re.compile(
    r"self\.__next_f\.push\(\[1,(\".*?\")\]\)", re.DOTALL
)


class MatchPageParseError(RuntimeError):
    """試合ページの構造が想定と異なり、必要な情報が抜き出せなかった場合に送出する。"""


@dataclass
class GoalEvent:
    team: str            # 得点したチーム名 (例: "清水エスパルス")
    player: str          # 得点者名
    minute: str          # 得点時刻 (例: "38'")
    own_goal: bool
    player_href: str | None = None


@dataclass
class CardEvent:
    team: str             # カードを受けたチーム名
    player: str            # 選手名
    position: str | None   # ポジション (例: "DF 14")
    minute: str            # 分 (例: "84'")
    card_type: str          # "yellow" / "red" / "yellow_yellow_red" など jleague.jp の表記そのまま


@dataclass
class MatchResult:
    url: str
    section: str                 # 節 (例: "第1節")
    kickoff_iso: str | None      # ISO8601形式のキックオフ日時
    stadium: str | None
    attendance: int | None
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    goals: list[GoalEvent] = field(default_factory=list)
    cards: list[CardEvent] = field(default_factory=list)

    def summary_line(self) -> str:
        return f"{self.home_team} {self.home_score}-{self.away_score} {self.away_team}"

    def to_dict(self) -> dict:
        """後続ステップ (ブログ側のスクレイピング結果とマージしてClaude APIに渡す等) 用に
        dataclassを素のdict/JSONへ変換する。"""
        return {
            "url": self.url,
            "section": self.section,
            "kickoff_iso": self.kickoff_iso,
            "stadium": self.stadium,
            "attendance": self.attendance,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "goals": [g.__dict__ for g in self.goals],
            "cards": [c.__dict__ for c in self.cards],
        }


def fetch_html(url: str, timeout: int = 20) -> str:
    """試合結果ページの生HTMLを取得する。"""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def _extract_next_f_chunks(html: str) -> dict[str, object]:
    """
    HTML中の self.__next_f.push(...) チャンクを全て取り出し、
    "<16進chunkId>:<データ>" の形式をパースして
    { chunkId: パース済みオブジェクト or 生文字列 } の辞書にして返す。

    データ部分が "[" か "{" で始まる場合は JSON としてパースする
    (先頭に "I"・"HL" などの型プレフィックスが付く行はJSONではないので生文字列のまま保持する)。
    """
    chunks: dict[str, object] = {}
    for m in _NEXT_F_PATTERN.finditer(html):
        js_string_literal = m.group(1)
        # JSの文字列エスケープ(\" \\ \n \uXXXX 等)はJSONの文字列エスケープと同じなので
        # そのまま json.loads に通してデコードできる。
        try:
            payload = json.loads(js_string_literal)
        except json.JSONDecodeError:
            continue

        # 1つの <script> タグ (1回の push呼び出し) の中に、
        # "<chunkId>:<data>\n<chunkId>:<data>\n..." という形で
        # 複数行(複数チャンク)がまとめて詰め込まれていることがあるので、
        # 改行で分割してから1行ずつ処理する。
        # (これをせず全体を1個のチャンクとして json.loads すると、
        #  2行目以降が余計な文字列として付いてきて "Extra data" エラーになり、
        #  そのチャンクが構造化データとして読めなくなってしまう)
        for line in payload.split("\n"):
            chunk_id, sep, data = line.partition(":")
            if not sep:
                continue
            data = data.strip()
            if data[:1] in "[{":
                try:
                    chunks[chunk_id] = json.loads(data)
                    continue
                except json.JSONDecodeError:
                    pass
            chunks[chunk_id] = data
    return chunks


# 例: "widget-84'-26" / "widget-container-45+2'-3" から分 ("84'" / "45+2'") を取り出す。
# 各プレーバイプレー・イベントの一番外側の要素にだけ付くidなので、
# これにマッチした時だけ「現在の分」を更新する（内側の "card-0-選手名" のような
# idで上書きされないようにするため）。
_MINUTE_FROM_WIDGET_ID = re.compile(r"^widget(?:-container)?-(.+?)-\d+$")


def _walk(node, minute: str | None = None):
    """
    パース済みチャンクのネスト構造 (list/dict混在) を再帰的に辿るジェネレータ。
    ["$", "div", "widget-84'-26", {...props}] のような
    プレーバイプレー・イベントのルート要素を見つけたら、
    そのidから分を抜き出して以降の子要素すべてに伝播させる
    (内側の別のid文字列では上書きしない)。

    yield するのは (node, 現在の分文字列 or None) のタプル。
    """
    current_minute = minute
    if (
        isinstance(node, list)
        and len(node) >= 3
        and node[0] == "$"
        and isinstance(node[2], str)
    ):
        m = _MINUTE_FROM_WIDGET_ID.match(node[2])
        if m:
            current_minute = m.group(1)

    yield node, current_minute

    if isinstance(node, list):
        for item in node:
            yield from _walk(item, current_minute)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk(value, current_minute)


def _find_game_header(chunks: dict[str, object]) -> dict:
    """
    homeTeam/awayTeam/score/stadium/date などを含む
    試合ヘッダーのdict ("variant":"game-details") を全チャンクの中から探す。
    """
    for chunk in chunks.values():
        for node, _ in _walk(chunk):
            if (
                isinstance(node, dict)
                and node.get("variant") == "game-details"
                and "homeTeam" in node
                and "awayTeam" in node
            ):
                return node
    raise MatchPageParseError(
        "試合ヘッダー ('variant':'game-details' を含むブロック) が見つかりませんでした。"
        "jleague.jp側でページ構造が変更された可能性があります。"
    )


def _extract_goals_from_header(header: dict) -> list[GoalEvent]:
    goals: list[GoalEvent] = []
    for side in ("homeTeam", "awayTeam"):
        team = header[side]
        team_name = team.get("name", "")
        for scorer in team.get("playerScoreList", []) or []:
            goals.append(
                GoalEvent(
                    team=team_name,
                    player=scorer.get("name", ""),
                    minute=scorer.get("scoreTime", ""),
                    own_goal=bool(scorer.get("ownGoal", False)),
                    player_href=scorer.get("href"),
                )
            )
    # 時刻順に並べ替え (例: "38'" -> 38, "45+2'" -> 45.2 くらいの簡易ソート)
    def _minute_key(g: GoalEvent) -> tuple[int, int]:
        digits = re.match(r"(\d+)(?:\+(\d+))?", g.minute)
        if not digits:
            return (999, 0)
        base = int(digits.group(1))
        extra = int(digits.group(2)) if digits.group(2) else 0
        return (base, extra)

    goals.sort(key=_minute_key)
    return goals


def _find_cards(chunks: dict[str, object]) -> list[CardEvent]:
    """
    プレーバイプレー内の各イベントウィジェットを走査し、
    "cardType" を持つ要素 (イエロー/レッドカード) だけを抜き出す。
    分は、そのカード要素が属しているウィジェットのidから取得する。
    """
    cards: list[CardEvent] = []
    seen: set[tuple] = set()  # 同じチャンクが複数箇所に重複して現れることがあるため重複排除用

    for chunk in chunks.values():
        for node, minute in _walk(chunk):
            if not (isinstance(node, dict) and "cardType" in node and "playerName" in node):
                continue
            minute = minute or ""
            key = (node.get("playerName"), node.get("cardType"), minute, node.get("teamName"))
            if key in seen:
                continue
            seen.add(key)
            cards.append(
                CardEvent(
                    team=node.get("teamName", ""),
                    player=node.get("playerName", ""),
                    position=node.get("playerPosition"),
                    minute=minute,
                    card_type=node.get("cardType", ""),
                )
            )

    # 同じ分に複数枚のカードが出た場合 (例: 61分に警告→直後に2枚目の警告=退場)、
    # "1枚目の警告" が先に来るように、カード種別にも副次的な並び順を付ける。
    _CARD_TYPE_ORDER = {"yellow": 0, "red": 1, "yellow-yellow-red": 2, "yellow_yellow_red": 2}

    def _minute_key(c: CardEvent) -> tuple[int, int, int]:
        digits = re.match(r"(\d+)(?:\+(\d+))?", c.minute)
        if not digits:
            return (999, 0, 0)
        base = int(digits.group(1))
        extra = int(digits.group(2)) if digits.group(2) else 0
        return (base, extra, _CARD_TYPE_ORDER.get(c.card_type, 1))

    cards.sort(key=_minute_key)
    return cards


def parse_match_result(html: str, url: str = "") -> MatchResult:
    """生HTML文字列から MatchResult を組み立てる。"""
    chunks = _extract_next_f_chunks(html)
    if not chunks:
        raise MatchPageParseError(
            "self.__next_f.push チャンクが1件も見つかりませんでした。"
            "アクセスがブロックされている(bot対策等)か、ページ構造が変わった可能性があります。"
        )

    header = _find_game_header(chunks)

    stadium_info = header.get("stadium") or {}
    date_raw = header.get("date")  # 例: "$D2026-08-08T10:00:00.000Z"
    kickoff_iso = date_raw[2:] if isinstance(date_raw, str) and date_raw.startswith("$D") else date_raw

    result = MatchResult(
        url=url,
        section=header.get("section", ""),
        kickoff_iso=kickoff_iso,
        stadium=stadium_info.get("name"),
        attendance=stadium_info.get("numberOfPeople"),
        home_team=header["homeTeam"].get("name", ""),
        away_team=header["awayTeam"].get("name", ""),
        home_score=header["homeTeam"].get("score", 0),
        away_score=header["awayTeam"].get("score", 0),
        goals=_extract_goals_from_header(header),
        cards=_find_cards(chunks),
    )
    return result


def get_match_result(url: str) -> MatchResult:
    """試合結果ページURLを渡すと MatchResult を返す(取得+パースを一括で行う)。"""
    html = fetch_html(url)
    return parse_match_result(html, url=url)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.jleague.jp/match/j1/2026/080803/"
    match = get_match_result(target_url)

    # data/match_result.json に保存しておく(次のステップ = Claude APIへの入力として使う想定)
    out_path = Path(__file__).resolve().parent.parent / "data" / "match_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(match.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[saved] {out_path}")

    print("=" * 60)
    print(f"{match.section}  {match.kickoff_iso}")
    print(f"{match.stadium}  観客数: {match.attendance}")
    print(match.summary_line())
    print("-" * 60)
    print("【得点者】")
    for g in match.goals:
        og = "(OG)" if g.own_goal else ""
        print(f"  {g.minute:>6}  {g.team}  {g.player} {og}")
    print("-" * 60)
    print("【警告・退場】")
    if not match.cards:
        print("  なし")
    CARD_LABELS = {
        "yellow": "警告",
        "red": "退場",
        # jleague.jpの実データは "yellow-yellow-red" (ハイフン区切り)、
        # UIラベル辞書側は "yellow_yellow_red" (アンダースコア区切り) と表記ゆれがあるため両方見る。
        "yellow-yellow-red": "警告2枚目(退場)",
        "yellow_yellow_red": "警告2枚目(退場)",
    }
    for c in match.cards:
        label = CARD_LABELS.get(c.card_type, c.card_type)
        print(f"  {c.minute:>6}  {c.team}  {c.player} ({c.position})  {label}")
    print("=" * 60)
