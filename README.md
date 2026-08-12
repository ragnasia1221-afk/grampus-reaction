# 名古屋グランパス 反応まとめサイト

Jリーグ公式サイト・ドメサカブログ・5ch実況スレッドから名古屋グランパスの試合結果と
サポーターの反応を自動収集し、Claude APIでまとめ記事を生成してGitHub Pagesに公開する。

**公開サイト**: `docs/` 配下がGitHub Pagesで公開される([有効化後はこちら](https://ragnasia1221-afk.github.io/grampus-reaction/))

## パイプライン構成

```
src/discover_match.py     試合結果ページと対応するドメサカブログ記事を自動検出
src/jleague_scraper.py    Jリーグ公式サイト → スコア・得点者・警告退場
src/domesoccer_scraper.py ドメサカブログ → 掲示板コメント・引用ツイート
src/nichan_scraper.py     5ch実況スレッド → 試合実況の投稿
src/generate_article.py   Claude API (Sonnet 5) → まとめ文・引用選定(著作権ルール検証つき)
src/render_html.py        Jinja2テンプレート → 最終HTML
src/run_pipeline.py       上記すべてを1回分実行するオーケストレーター
```

## 著作権ルール

掲示板コメント・Xポスト・5ch投稿からの直接引用は、1ソースにつき15語程度・1回まで。
それ以外は要約・言い換えのみで構成する。生成された引用が元テキストの逐語部分文字列に
なっているかをコード側でも機械的に検証し、確認できない場合は自動的に無効化する
(`generate_article.py` の `verify_quotes`)。

## ローカルでの手動実行

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
cd src
python run_pipeline.py
```

直近の消化試合を自動検出し、`docs/{gameId}.html` が既に存在する場合は何もしない
(重複生成防止・cron実行に対して冪等)。

## 自動更新

`.github/workflows/daily.yml` により毎日定時にGitHub Actions上で `run_pipeline.py` を実行し、
新しい試合が見つかれば `docs/` にページを追加してコミット・プッシュする
(`ANTHROPIC_API_KEY` はリポジトリのSecretsに設定)。
