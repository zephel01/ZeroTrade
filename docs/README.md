# ZeroTrade ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| [tools.md](tools.md) | コマンド一覧、設定、日々の運用、トラブルシューティング |
| [backtesting.md](backtesting.md) | 検証の進め方と、**自分を騙さないための7項目** |
| [strategies.md](strategies.md) | 同梱戦略の中身、実測成績、新しい戦略の書き方 |
| [hypotheses.md](hypotheses.md) | **検証する仮説の事前登録と結果**（当たり外れの全記録） |
| [symbols.md](symbols.md) | 銘柄ごとの取引条件と検証結果（SOL / PEPE / TAO / 金 / 原油） |
| [bingx-plan.md](bingx-plan.md) | **BingX でのテスト計画**（配管テスト→前向き検証→実弾の段取り） |
| [forward-test.md](forward-test.md) | 実施中の前向き検証（6銘柄・判定基準は開始前に確定） |
| [plan.md](plan.md) | 当初の仕様書 |

## はじめての人へ

```bash
pip install -e ".[dev,ui]"
zerotrade -c config/paper.yaml check      # 設定確認（発注しない）
zerotrade -c config/paper.yaml run --iterations 300
```

実際に発注するのは `run` だけで、他のコマンドはすべて安全に試せる。

## 読む順番

まず [tools.md](tools.md) でコマンドを把握し、次に [backtesting.md](backtesting.md) で検証の作法を読む。戦略に手を入れるなら [strategies.md](strategies.md) へ。

新しい戦略を思いついたときは、コードを書く前に [hypotheses.md](hypotheses.md) を読むこと。**先に仮説を書いてから測る**という手順と、これまで何を試して何が外れたかの全記録がある。

**実弾を入れる前には [backtesting.md の「自分を騙さないための7項目」](backtesting.md#自分を騙さないための7項目) を必ず読むこと。** このリポジトリで実際に起きた失敗（開発用データで +18.8% を出した戦略が検定用データで -7.4% に転落した件）が、なぜ起きたかも含めて書いてある。
