# scripts/

`zerotrade` コマンドに載せるほど汎用でない作業を置いてある。**いずれも発注しない。**

すべてリポジトリのルートから実行する。

| スクリプト | 用途 |
|-----------|------|
| `forward_start.sh` | 前向き検証をまとめて起動（引数: グループ名、既定 `forward`） |
| `forward_status.sh` | プロセスの生存確認 |
| `forward_watch.py` | 建玉・確定損益・件数を1画面で表示（`--group` / `--watch`） |
| `forward_judge.py` | 進捗と、規定件数到達後の合否判定（`--group`） |
| `fetch_bingx_public.py` | BingX 公開APIから1時間足を取得（認証不要・銘柄を引数で指定） |
| `fetch_bitstamp.py` | Bitstamp から BTC/USD を2017年以降取得（検定用の独立データ） |
| `survey_symbols.py` | 複数銘柄を前半/後半に分けて横並び検証 |
| `hypothesis_weekend.py` | H1 週末効果の検定（プラセボ付き） |
| `hypothesis_funding.py` | H2 ファンディング時刻の検定 |
| `hypothesis_monthend.py` | H3 月末リバランスの検定 |

```bash
# 前向き検証
scripts/forward_start.sh
python3 scripts/forward_watch.py
python3 scripts/forward_judge.py

# データ取得（data/ は git 管理外なので、必要になったら取り直す）
python3 scripts/fetch_bingx_public.py SOL-USDT 1000PEPE-USDT TAO-USDT
python3 scripts/fetch_bitstamp.py

# 検証の再現
python3 scripts/survey_symbols.py
python3 scripts/hypothesis_weekend.py
```

`hypothesis_*.py` は [../docs/hypotheses.md](../docs/hypotheses.md) の事前登録に対応する。
**外れた仮説も含めて再現できる形で残してある。**

詳しい説明は [../docs/tools.md の「スクリプト一覧」](../docs/tools.md#スクリプト一覧) にある。
