"""
ステップ1(jleague_scraper.py)・ステップ2(domesoccer_scraper.py)・
2b(nichan_scraper.py)で取得したデータをClaude API(Claude Sonnet 5)に渡し、
  - まとめ記事の要約文(完全に要約・言い換え。原文の直接引用はしない)
  - 掲示板コメント / Xポスト / 5ch実況スレッドからの「引用選定」
      引用ルール: 1ソースにつき短い逐語引用(目安15語程度)を1回まで。
                  それ以外の文脈は要約・言い換えで表現する。
を生成する。

著作権対応の要点:
  - Claudeに「原文からの逐語引用は1件につき短く1回まで」と明示的に指示する。
  - さらに、生成された quote が実際に元テキストの部分文字列になっているか
    (＝Claudeが引用を捏造していないか)をコード側で機械的に検証する。
    検証に失敗した場合は、その引用を安全側に倒して無効化し(paraphraseのみ残す)、
    警告としてログに出す。これはあくまでベストエフォートのチェックであり、
    公開前に人の目でざっと確認することを推奨する。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

import anthropic

MODEL_ID = "claude-sonnet-5"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MATCH_RESULT_PATH = DATA_DIR / "match_result.json"
BLOG_REACTIONS_PATH = DATA_DIR / "blog_reactions.json"
NICHAN_REACTIONS_PATH = DATA_DIR / "nichan_reactions.json"  # 無ければスキップ(任意ソース)
OUTPUT_PATH = DATA_DIR / "generated_article.json"


# ---------------------------------------------------------------------------
# Claudeへの構造化出力スキーマ (Pydantic)
# ---------------------------------------------------------------------------

class BoardPick(BaseModel):
    comment_id: str  # 入力データの comment_id と完全一致させること
    affiliation: Literal["home", "away", "other"]
    paraphrase: str  # 要約・言い換え(本文の主旨を自分の言葉でまとめたもの。引用不可)
    quote: str  # 原文からの短い逐語引用(15語程度まで)。1コメントにつき1個だけ。


class TweetPick(BaseModel):
    tweet_url: str  # 入力データの tweet_url と完全一致させること
    paraphrase: str
    quote: str


class NichanPick(BaseModel):
    post_number: int  # 入力データの number と完全一致させること
    paraphrase: str
    quote: str


class GeneratedArticle(BaseModel):
    summary_paragraphs: list[str]  # まとめ記事の本文(2段落程度)。要約・言い換えのみ、引用禁止
    board_picks: list[BoardPick]
    tweet_picks: list[TweetPick]
    nichan_picks: list[NichanPick]


# ---------------------------------------------------------------------------
# プロンプト構築
# ---------------------------------------------------------------------------

# このサイトが「ファン視点」でまとめるクラブ。home_team/away_teamのどちらかがこれと一致する試合は
# そのクラブのサポーター寄りの選定になり、一致しない試合(=このクラブが関与しない試合)は
# 中立視点でまとめる。Noneにすると常にすべての試合を中立視点で扱う。
FOCUS_CLUB: str | None = "名古屋グランパス"

_BASE_SYSTEM_PROMPT = """\
あなたはJリーグの試合反応まとめブログの編集者です。
試合結果とファンの反応をまとめた記事を作成します。

# 著作権ルール(厳守)
- 掲示板コメント・Xポスト・5ch投稿の原文を長く引用してはいけません。
- 各コメント/ポスト/投稿につき、使ってよい直接引用(quote)は「15語程度以内の短いフレーズを1回だけ」です。
  quote には、渡された原文の一部をそのまま(一字一句変えずに)使ってください。要約・改変・意訳をquoteに混ぜないこと。
- quote 以外の部分(paraphrase および記事本文の summary_paragraphs)では、
  原文の言葉をそのまま使わず、必ず自分の言葉で要約・言い換えてください。
- summary_paragraphs には原文からの直接引用を一切含めないでください(完全な言い換えのみ)。
"""

_COMMON_SELECTION_POLICY_WITH_NICHAN = """\
- tweet_picks: 試合後の反応として代表的なものを選ぶ。
- nichan_picks: 5chの実況スレッドはノイズ(相槌・単発の煽り・脱線)が多いので、
  戦術面の具体的な指摘や、得点・退場など試合の展開に直接反応している、内容のある投稿だけを選ぶこと。
  「乙」「わろた」のような単なる相槌・感嘆だけの投稿は選ばないこと。
- comment_id / tweet_url / post_number は必ず渡されたデータのものと完全に一致させること。存在しないIDを作らないこと。
- 各ソースから選ぶ件数の目安: board_picks 8〜10件、tweet_picks 4〜6件、nichan_picks 5〜8件。
"""

_COMMON_SELECTION_POLICY_NO_NICHAN = """\
- tweet_picks: 試合後の反応として代表的なものを選ぶ。
- 5chの投稿データは今回渡されていません。nichan_picks は必ず空配列 [] にしてください
  (存在しないIDを作って埋めてはいけません)。
- comment_id / tweet_url は必ず渡されたデータのものと完全に一致させること。存在しないIDを作らないこと。
- 各ソースから選ぶ件数の目安: board_picks 8〜10件、tweet_picks 4〜6件。
"""


def _names_match(a: str | None, b: str | None) -> bool:
    """全角/半角の表記ゆれ(例: jleague.jpの"横浜Ｆ・マリノス"の全角Ｆ)を吸収した比較。"""
    if a is None or b is None:
        return False
    return unicodedata.normalize("NFKC", a) == unicodedata.normalize("NFKC", b)


def _fan_side(focus_club: str | None, home_team: str, away_team: str) -> str | None:
    """focus_clubがこの試合のhome/awayどちらかを返す。関与していなければNone(=中立視点)。"""
    if _names_match(focus_club, home_team):
        return "home"
    if _names_match(focus_club, away_team):
        return "away"
    return None


def _build_system_prompt(
    focus_club: str | None, home_team: str, away_team: str, has_nichan: bool
) -> str:
    side = _fan_side(focus_club, home_team, away_team)

    if side == "home":
        fan_team, other_team = home_team, away_team
    elif side == "away":
        fan_team, other_team = away_team, home_team
    else:
        fan_team = other_team = None

    if fan_team:
        policy = f"""\
# 選定方針(ファン視点: {fan_team})
- このサイトは{fan_team}のサポーター向けです。board_picks は{fan_team}サポーターの反応を中心に、
  称賛・批判・分析など多様な視点が伝わるように選ぶこと。
  {other_team}サポーターの反応も対比として少数、他クラブ(other)視点があれば1件程度含めてよい。
  同じような内容の重複は避け、それぞれ違う論点のコメントを選ぶこと。
"""
    else:
        policy = """\
# 選定方針(中立視点)
- このサイトは特定クラブに肩入れしません。board_picks は両チームのサポーターの反応を
  概ね均等な件数でバランス良く選び、称賛・批判・分析など多様な視点が伝わるようにすること。
  一方のチームの反応に偏らないよう注意すること。他クラブ(other)視点があれば1件程度含めてよい。
"""

    selection_policy = (
        _COMMON_SELECTION_POLICY_WITH_NICHAN if has_nichan else _COMMON_SELECTION_POLICY_NO_NICHAN
    )
    return _BASE_SYSTEM_PROMPT + "\n" + policy + selection_policy


def _build_user_content(match: dict, reactions: dict, nichan: dict | None) -> str:
    goals_text = "\n".join(
        f"  {g['minute']} {g['team']} {g['player']}" + ("(OG)" if g.get("own_goal") else "")
        for g in match.get("goals", [])
    ) or "  なし"

    cards_text = "\n".join(
        f"  {c['minute']} {c['team']} {c['player']}({c.get('position')}) {c['card_type']}"
        for c in match.get("cards", [])
    ) or "  なし"

    comments_json = json.dumps(
        [
            {
                "comment_id": c["comment_id"],
                "affiliation": c["affiliation"],
                "text": c["text"],
            }
            for c in reactions.get("comments", [])
        ],
        ensure_ascii=False,
        indent=2,
    )

    tweets_json = json.dumps(
        [
            {
                "tweet_url": t["url"],
                "author_name": t["author_name"],
                "handle": t["handle"],
                "text": t["text"],
            }
            for t in reactions.get("tweets", [])
        ],
        ensure_ascii=False,
        indent=2,
    )

    nichan_section = ""
    if nichan and nichan.get("posts"):
        nichan_json = json.dumps(
            [
                {"number": p["number"], "posted_at": p["posted_at"], "text": p["text"]}
                for p in nichan["posts"]
            ],
            ensure_ascii=False,
            indent=2,
        )
        nichan_section = f"""
## 5ch実況スレッド投稿一覧 (この中からnichan_picksを選ぶこと。post_numberは必ずこの中のnumberと一致させる)
スレッドタイトル: {nichan.get('title', '')}
{nichan_json}
"""

    return f"""\
## 試合情報
{match.get('home_team')} {match.get('home_score')} - {match.get('away_score')} {match.get('away_team')}
{match.get('section')} / {match.get('stadium')} / {match.get('kickoff_iso')}

得点:
{goals_text}

警告・退場:
{cards_text}

## 掲示板コメント一覧 (この中からboard_picksを選ぶこと。comment_idは必ずこの中のものを使う)
{comments_json}

## Xポスト一覧 (この中からtweet_picksを選ぶこと。tweet_urlは必ずこの中のものを使う)
{tweets_json}
{nichan_section}
上記データをもとに、著作権ルールと選定方針に従って記事を生成してください。
"""


# ---------------------------------------------------------------------------
# 引用の検証(Claudeの出力を鵜呑みにせず、原文に実在するかコードで確認する)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """全角/半角や空白の差異を吸収して比較しやすくする。"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    return text


def _estimate_word_count(text: str) -> float:
    """
    「15語程度」の簡易見積もり。
    英数字主体の文はスペース区切りの語数、日本語主体の文は文字数から概算する
    (日本語は分かち書きされないため厳密な語数計算はできない。あくまで目安)。
    """
    text = text.strip()
    if not text:
        return 0.0
    ascii_ratio = sum(1 for c in text if c.isascii()) / len(text)
    if ascii_ratio > 0.5:
        return float(len(text.split()))
    return len(text) / 1.5  # 日本語の大雑把な目安: 1語 ≈ 1.5文字


@dataclass
class VerificationResult:
    ok_board_picks: list[BoardPick] = field(default_factory=list)
    ok_tweet_picks: list[TweetPick] = field(default_factory=list)
    ok_nichan_picks: list[NichanPick] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _verify_quote(
    quote: str,
    source_text: str,
    label: str,
    max_word_estimate: float,
    warnings: list[str],
) -> str:
    """
    quoteが元テキストの逐語引用として実在するか、長さが目安内かをチェックする。
    捏造の疑いがあれば空文字を返す(=呼び出し側でparaphraseのみ表示にフォールバックする)。
    """
    if not quote:
        return quote
    if _normalize(quote) not in _normalize(source_text):
        warnings.append(
            f"[引用を無効化] {label} の quote が原文の逐語引用として確認できませんでした"
            f"(捏造の可能性)。quoteを空にして要約のみ残します。  quote={quote!r}"
        )
        return ""
    if _estimate_word_count(quote) > max_word_estimate:
        warnings.append(
            f"[要確認] {label} の quote が15語ルールの目安を超えている可能性があります。"
            f"公開前に手動確認してください。  quote={quote!r}"
        )
    return quote


def verify_quotes(
    article: GeneratedArticle,
    comments_by_id: dict[str, str],
    tweets_by_url: dict[str, str],
    nichan_by_number: dict[int, str],
    max_word_estimate: float = 18.0,  # 15語ルールに少しだけ余裕を持たせた閾値
) -> VerificationResult:
    result = VerificationResult()

    for pick in article.board_picks:
        source = comments_by_id.get(pick.comment_id)
        if source is None:
            result.warnings.append(
                f"[却下] board_pick comment_id={pick.comment_id!r} は元データに存在しません。破棄します。"
            )
            continue
        quote = _verify_quote(
            pick.quote, source, f"comment_id={pick.comment_id!r}", max_word_estimate, result.warnings
        )
        result.ok_board_picks.append(pick.model_copy(update={"quote": quote}))

    for pick in article.tweet_picks:
        source = tweets_by_url.get(pick.tweet_url)
        if source is None:
            result.warnings.append(
                f"[却下] tweet_pick tweet_url={pick.tweet_url!r} は元データに存在しません。破棄します。"
            )
            continue
        quote = _verify_quote(
            pick.quote, source, f"tweet_url={pick.tweet_url!r}", max_word_estimate, result.warnings
        )
        result.ok_tweet_picks.append(pick.model_copy(update={"quote": quote}))

    for pick in article.nichan_picks:
        source = nichan_by_number.get(pick.post_number)
        if source is None:
            result.warnings.append(
                f"[却下] nichan_pick post_number={pick.post_number} は元データに存在しません。破棄します。"
            )
            continue
        quote = _verify_quote(
            pick.quote, source, f"5ch #{pick.post_number}", max_word_estimate, result.warnings
        )
        result.ok_nichan_picks.append(pick.model_copy(update={"quote": quote}))

    return result


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def generate_article(
    match: dict,
    reactions: dict,
    nichan: dict | None = None,
    focus_club: str | None = FOCUS_CLUB,
) -> tuple[GeneratedArticle, VerificationResult]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY が設定されていません。"
            "環境変数にAPIキーを設定してから再実行してください。"
        )

    client = anthropic.Anthropic()

    user_content = _build_user_content(match, reactions, nichan)
    has_nichan = bool(nichan and nichan.get("posts"))
    system_prompt = _build_system_prompt(
        focus_club, match["home_team"], match["away_team"], has_nichan
    )

    response = client.messages.parse(
        model=MODEL_ID,
        max_tokens=12000,
        # このタスクは選定・要約作業で複雑な多段推論は不要なため、thinkingを明示的に無効化し
        # max_tokens予算を構造化出力(JSON)側にフル活用する
        # (Sonnet 5はthinking未指定だとデフォルトでadaptive thinkingが動き、
        #  その分の出力がmax_tokensを圧迫して本文JSONが途中で切れる事故があったため)。
        thinking={"type": "disabled"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        output_format=GeneratedArticle,
    )
    article = response.parsed_output
    if article is None:
        raise RuntimeError("Claudeの応答を構造化データとしてパースできませんでした。")

    comments_by_id = {c["comment_id"]: c["text"] for c in reactions.get("comments", [])}
    tweets_by_url = {t["url"]: t["text"] for t in reactions.get("tweets", [])}
    nichan_by_number = {p["number"]: p["text"] for p in (nichan or {}).get("posts", [])}
    verification = verify_quotes(article, comments_by_id, tweets_by_url, nichan_by_number)

    return article, verification


if __name__ == "__main__":
    if not MATCH_RESULT_PATH.exists() or not BLOG_REACTIONS_PATH.exists():
        print(
            "先に jleague_scraper.py と domesoccer_scraper.py を実行して "
            f"{MATCH_RESULT_PATH.name} / {BLOG_REACTIONS_PATH.name} を用意してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    match_data = json.loads(MATCH_RESULT_PATH.read_text(encoding="utf-8"))
    reactions_data = json.loads(BLOG_REACTIONS_PATH.read_text(encoding="utf-8"))
    nichan_data = (
        json.loads(NICHAN_REACTIONS_PATH.read_text(encoding="utf-8"))
        if NICHAN_REACTIONS_PATH.exists()
        else None
    )
    if nichan_data is None:
        print(f"[情報] {NICHAN_REACTIONS_PATH.name} が無いため、5chソースなしで生成します。")

    article, verification = generate_article(match_data, reactions_data, nichan_data)

    output = {
        "summary_paragraphs": article.summary_paragraphs,
        "board_picks": [p.model_dump() for p in verification.ok_board_picks],
        "tweet_picks": [p.model_dump() for p in verification.ok_tweet_picks],
        "nichan_picks": [p.model_dump() for p in verification.ok_nichan_picks],
    }
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[saved] {OUTPUT_PATH}")

    print("=" * 60)
    print("【まとめ】")
    for p in article.summary_paragraphs:
        print(p)
        print()
    print("-" * 60)
    print(f"【掲示板コメント選定 {len(verification.ok_board_picks)}件】")
    for pick in verification.ok_board_picks:
        print(f"  [{pick.affiliation}] {pick.comment_id}")
        print(f"    要約: {pick.paraphrase}")
        if pick.quote:
            print(f"    引用: 「{pick.quote}」")
    print("-" * 60)
    print(f"【Xポスト選定 {len(verification.ok_tweet_picks)}件】")
    for pick in verification.ok_tweet_picks:
        print(f"  {pick.tweet_url}")
        print(f"    要約: {pick.paraphrase}")
        if pick.quote:
            print(f"    引用: 「{pick.quote}」")
    print("-" * 60)
    print(f"【5ch実況選定 {len(verification.ok_nichan_picks)}件】")
    for pick in verification.ok_nichan_picks:
        print(f"  #{pick.post_number}")
        print(f"    要約: {pick.paraphrase}")
        if pick.quote:
            print(f"    引用: 「{pick.quote}」")

    if verification.warnings:
        print("-" * 60)
        print("【検証ワーニング】(公開前に確認してください)")
        for w in verification.warnings:
            print(f"  - {w}")
    print("=" * 60)
