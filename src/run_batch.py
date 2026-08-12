"""
J1全クラブ分をまとめて処理する一括実行スクリプト(手動の一回きり実行用)。

現在(2026年8月12日時点)は全クラブの「直近消化試合」がちょうどJ1第1節と一致するため、
20クラブ全部についてrun_pipeline.run(team_slug)を呼べば、結果的にJ1第1節の全カードが
生成される(同じ試合は両クラブから発見されるが、docs/{gameId}.htmlの存在チェックで
自動的に重複排除される)。

1クラブの失敗が他クラブの処理を止めないよう、例外はログに残して次へ進む。
"""

from __future__ import annotations

import traceback

import run_pipeline

# 2026年J1全20クラブのjleague.jp club slug (2026-08-12 J1順位表ページから確認済み)
J1_CLUB_SLUGS = [
    "nagoya", "shimizu", "kashima", "yokohamafm", "urawa", "kashiwa",
    "ftokyo", "tokyov", "kawasakif", "kobe", "kyoto", "gosaka", "cosaka",
    "hiroshima", "okayama", "fukuoka", "machida", "chiba", "mito", "nagasaki",
]


def main() -> None:
    results: dict[str, str] = {}
    for slug in J1_CLUB_SLUGS:
        run_pipeline.log(f"===== {slug} 開始 =====")
        try:
            run_pipeline.run(team_slug=slug)
            results[slug] = "OK"
        except Exception as e:
            results[slug] = f"ERROR: {e}"
            run_pipeline.log(f"[エラー] {slug} の処理に失敗しました: {e}")
            traceback.print_exc()

    run_pipeline.log("===== 全クラブ処理完了 =====")
    for slug, status in results.items():
        run_pipeline.log(f"  {slug}: {status}")


if __name__ == "__main__":
    main()
