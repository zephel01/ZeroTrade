<div align="center">

# ZeroTrade

**裁量でルールを破れない自動売買基盤 — 仮想通貨・FX**

「今日は調子がいいからロットを上げる」を、**コードレベルで不可能**にする。

[![CI](https://github.com/zephel01/ZeroTrade/actions/workflows/ci.yml/badge.svg)](https://github.com/zephel01/ZeroTrade/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](#ライセンス)
[![Tests](https://img.shields.io/badge/tests-433%20passed-brightgreen.svg)](tests/)
[![Typed: strict](https://img.shields.io/badge/mypy-strict-informational.svg)](pyproject.toml)

[クイックスタート](#クイックスタート) ·
[設計の芯](#設計の芯--4つの譲れない前提) ·
[検証の作法](#検証の作法) ·
[実測結果](#実測結果--勝てる戦略はまだ見つかっていない) ·
[ドキュメント](#ドキュメント)

</div>

---

## これは何か

監視する時間が取れず、しかも裁量でルールを破ってしまう。その2つを**システムの構造で**防ぐための自動売買基盤である。

主な対象は**仮想通貨**で、BingX 専用アダプタと ccxt 経由の100以上の取引所に対応する。FX や株式もアダプタを足せば同じコア層で動く。ブローカー固有の事情は Adapter の内側に閉じ込めてあるので、**コア層は市場の違いを知らない**。

**このリポジトリのもう半分は「検証の作法」である。** 勝てる戦略は同梱していない。むしろ9つの候補を検証して8つを捨てた記録が、外れたものも含めて全部残っている。

## 設計の芯 — 4つの譲れない前提

|  | 前提 | 実装 |
|---|------|------|
| 1 | **戦略はシグナルしか出せない** | ロット決定も発注も戦略から触れない。裁量介入がコードレベルで不可能 |
| 2 | **全注文が RiskManager を通る** | `OrderManager.submit()` にリスク判定を迂回するパスが存在しない |
| 3 | **損失上限で自動停止する** | 状態を JSON に永続化。再起動しても上限がリセットされない。ただし決済注文だけは通る |
| 4 | **ブローカー差は Adapter に閉じる** | BingX の `positionSide` も、取引所ごとの銘柄記法も、コア層に漏れない |

3番目の「決済だけは通る」は意図的である。**建玉を閉じられない停止は、リスク管理として本末転倒**だからだ。

## クイックスタート

APIキーが無くてもそのまま動く。`PaperBroker` が疑似価格を生成する。

```bash
pip install -e ".[dev,ui]"

zerotrade -c config/paper.yaml check                  # 設定検証（発注しない）
zerotrade -c config/paper.yaml run --iterations 300   # ペーパートレードを300ループ
zerotrade -c config/paper.yaml dashboard              # TUI で監視（別ターミナル）
zerotrade -c config/paper.yaml report                 # HTMLレポートを書き出す
```

<details>
<summary><b>仮想通貨取引所につなぐ（BingX / ccxt）</b></summary>

```bash
pip install -e ".[ccxt]"
export BINGX_API_KEY="..." BINGX_API_SECRET="..."

zerotrade -c config/bingx.yaml check --connect   # 疎通・残高・気配値（発注しない）
```

`broker.exchange` を書き換えれば binance / bybit / okx など100以上の取引所が使える。BingX は専用アダプタがあり、片方向モードへの寄せ、`positionSide` の指定、銘柄リスト必須の建玉照会に対応している。

**APIキーには出金権限を絶対に付けないこと。** 取引権限だけで足りる。

</details>

<details>
<summary><b>実弾を止める仕掛け（<code>mode</code>）</b></summary>

`mode` は表示用のラベルではない。**実弾を止める仕掛けである。**

```
エラー: mode=paper のまま broker=bingx を environment=live で使うことはできません。
        この構成は実在の取引所へ本物の注文を送ります。
```

`mode: paper` のまま実在の取引所を `environment: live` で指定した構成は起動時に拒否される。逆に `mode: live` かつ `broker.name: paper` も拒否される。どちらも「設定に書いてあることと実際に起きることが食い違う」組み合わせだからである。

</details>

## 主な機能

| | 説明 |
|---|------|
| **リスク強制** | 11種のルールを RiskManager が強制。却下理由は機械可読な名前で返る |
| **バックテスト** | ライブと**同じ** StrategyRunner / RiskManager / OrderManager を通す |
| **過学習対策** | `optimize` が in-sample と out-of-sample を分けて自動で答え合わせ |
| **シャドー実行** | 本番の実勢価格で読み、約定だけ手元で模擬。**発注は外へ出ない** |
| **配管テスト** | `verify` が最小数量で発注経路を実物のAPIに対して検証する |
| **監視** | TUI ダッシュボードと自己完結HTMLレポート。取引プロセスとは別プロセス |
| **記録層** | SQLite（WAL）。トレード・注文・却下・シグナル・equity を残す |

すべて `Decimal`。float の丸め誤差が「口座の1%」という制約に混入するのを避けるため、float は指標計算の内部だけに限定している。

## 検証の作法

**このプロジェクトで一番時間を使ったのは、戦略を作ることではなく「自分を騙さない手順」を固めることだった。**

```
0. 仮説を書く      →  1. バックテスト  →  2. 事前登録  →  3. 前向き検証  →  4. 実弾
   （データを見る前）    （前半/後半に分割）   （基準を確定）    （未見データ）    （最小額）
```

各段階に「ここで落ちたら次へ進まない」関門がある。**落ちるのは失敗ではない。落ちずに実弾まで行くほうが危ない。**

<details>
<summary><b>なぜこの手順なのか</b></summary>

**仮説を先に書く。** 探して見つけたパターンは、たいてい探した本人にしか見えていない。判断の軸は「**消せない事情を持つ参加者がいるか**」である。価格を見て動く主体は、儲かると分かれば歪みを消しに来る。

**前半と後半に分ける。** 過去8回の失敗はほぼ全部「前半で良くて後半で崩れる」だった。掃引の上位が後半で全滅する現象は2回とも起きた。

**判定基準を先に確定する。** 結果を見てから基準を動かせば、どんな戦略でも合格させられる。事前登録には「言い訳の禁止リスト」まで書いてある。

**未見データで確かめる。** 過去データの分割は、どれだけ丁寧に分けても既に存在するデータの中の話である。今日から先のデータだけが本当の検定期間になる。

詳細は [docs/strategies.md の「採用までの道のり」](docs/strategies.md#書いたあと--採用までの道のり) と [docs/backtesting.md の「自分を騙さないための7項目」](docs/backtesting.md#自分を騙さないための7項目)。

</details>

## 実測結果 — 「勝てる戦略」はまだ見つかっていない

**都合の悪い結果も含めて全部残してある。** 通算は9候補中1つ。

| 対象 | 市場 | 結果 |
|------|------|------|
| `sma_rsi` | USD/JPY | 両期間でマイナス |
| `donchian` | USD/JPY | +18.8% → −7.4%（再現せず） |
| `donchian` | BTC | +5.03% → −3.23%（t=0.14） |
| パラメータ掃引 | 両市場 | 上位が検定期間で全滅 |
| 五十日 | USD/JPY | 2024で有意 → 2025-26で消滅 |
| H1 週末効果 | BTC | 符号反転、プラセボ以下 |
| H2 ファンディング時刻 | BTC | 符号反転、効果量 < スプレッド |
| H3 月末リバランス | BTC | 上昇ドリフトの誤認 |
| **`tokyo_fix`（仲値）** | **USD/JPY** | **+29.7% / +14.4%（両期間で再現）** |

唯一再現した仲値が他と違ったのは、**探して見つけたのではなく理屈を先に立てた**点にある。輸入企業は9:55のTTM仲値でドルを買う必要があり、価格が高かろうが安かろうが買う。価格から独立した、制度が生む需要がそこにある。

> **消されない歪みは、消せない事情を持つ参加者から生まれる。**

現在、6銘柄（BTC / SOL / 1000PEPE / TAO / 金 / 原油）で前向き検証を実施中。判定基準は開始前に確定済みで、[docs/forward-test.md](docs/forward-test.md) にある。**事前の予想は不合格**。

## 実物のAPIでしか見つからなかった不具合

モックとテストは全部通っていた。実際の取引所に**6.5ドル相当の注文を1回**出したら、これだけ出た。

| 不具合 | 放置していたら |
|--------|--------------|
| `supports_closed_trades` の誤申告 | **日次・週次の損失上限が永久に発動しない** |
| `strategy.granularity` が無い | H1で検証した戦略がM5で動く（別物） |
| ストップを `stopLossPrice` で送っていた | 新規建てが `position not exist` で弾かれる |
| 未確定足を戦略に渡していた | 足が閉じると消える「幻のシグナル」 |
| `reduce_only` 未対応 | 決済注文が反対側の**新規建て**になる |
| 永続契約のシンボル解決 | `BTC/USDT` は現物。swap は `BTC/USDT:USDT` |
| 足の取得上限（BingX 1440本） | 超えると**1本も返らない** |

**テストが通ることと、動くことは違う。** そのための `zerotrade verify` である。

## 構成

```
src/zerotrade/
├── models.py          # ドメインモデル（すべて Decimal）
├── settings.py        # YAML + 環境変数（pydantic で検証）
├── control.py         # キルスイッチ（停止方向にのみ作用）
├── verify.py          # 発注経路の検証（唯一、意図的に本物の注文を出す）
├── core/
│   ├── risk.py        # RiskManager ★中核
│   ├── sizing.py      # PositionSizer（ストップ距離からロットを逆算）
│   ├── orders.py      # OrderManager（発注の唯一の入口）
│   └── runner.py      # StrategyRunner（実行ループ）
├── brokers/
│   ├── base.py        # BaseBroker 抽象インターフェース
│   ├── paper.py       # 約定シミュレータ
│   ├── ccxt_broker.py # 100以上の仮想通貨取引所
│   ├── bingx.py       # BingX 専用（片方向モード・確定損益）
│   └── shadow.py      # 実勢価格で読み、約定は手元で模擬
├── data/              # フィード・取得・取り込み・整列
├── store/             # SQLite 記録層（WAL・別プロセスから読める）
├── strategies/        # Strategy 抽象 + 指標 + 同梱戦略
└── backtest/          # 本番と同じ経路を早送りする

scripts/               # 検証・運用スクリプト（いずれも発注しない）
docs/                  # 手順・事前登録・実測結果
```

## RiskManager が強制するルール

| ルール | 設定キー | 既定 |
|--------|----------|------|
| 1トレードあたりのリスク上限 | `max_risk_per_trade` | equity の 1% |
| 日次最大損失で自動停止 | `max_daily_loss` | 3% |
| 週次最大損失で自動停止 | `max_weekly_loss` | 6% |
| 証拠金使用率上限 | `max_margin_usage` | 30% |
| 同時保有ポジション数上限 | `max_open_positions` | 3 |
| 銘柄ごとの保有数上限 | `max_positions_per_symbol` | 1 |
| 1日の取引回数上限 | `max_daily_trades` | 20 |
| ストップ必須 | `require_stop_loss` | 有効 |
| 大相場検知（ATR急拡大） | `atr_spike_multiplier` | 平常時の3倍 |
| スプレッド上限 | `max_spread` | 無効 |
| 損切り後のクールダウン | `cooldown_seconds_after_loss` | 0秒 |

却下理由は `RiskDecision` のルール名として返る。ログ・通知・レポート・テストのすべてがこの名前をキーに動くので、「フィルタが厳しすぎるのか戦略が弱いのか」を切り分けられる。

> **スプレッドは検証結果を支配する。** USD/JPY の実データで、0.3銭なら −8%、2銭なら −48%。同じ戦略・同じデータで6倍の差が出る。銘柄ごとの実測値は [docs/symbols.md](docs/symbols.md)。

## 拡張する

<details>
<summary><b>新しいブローカーを足す</b></summary>

`BaseBroker` の抽象メソッド8個を実装し、`register_broker()` で登録して、設定の `broker.name` に名前を書く。コア層は一切変更しない。

正確な確定損益を返せるなら `get_closed_trades()` を実装して `supports_closed_trades = True` にする。返せない場合は StrategyRunner が建玉の差分から推定するので、損失上限は働き続ける。

**ただし `has` の申告を信じないこと。** ccxt が `fetchPositionsHistory: True` と申告しながら実装が無く `NotSupported` を投げた例がある。接続時に実際に叩いて確かめる。

</details>

<details>
<summary><b>新しい戦略を足す</b></summary>

```python
@register_strategy
class MyStrategy(Strategy):
    name = "my_strategy"
    warmup_bars = 50

    def generate(self, context: StrategyContext) -> Signal: ...
```

設定の `strategy.name` に書けば読み込まれる。戻せるのは `Signal` だけで、ロットも発注も触れない。`stop_loss` は既定で必須。

**コードが動くことと、その戦略を回してよいことは別である。** 採用までの関門は [docs/strategies.md](docs/strategies.md#書いたあと--採用までの道のり) に。

</details>

## 開発

```bash
pytest                              # 433件
ruff check . && ruff format --check .
mypy                                # strict
pre-commit install
```

CI は Python 3.11 / 3.12 で lint・型チェック・テストを回す。**ruff と mypy はバージョンを固定してある** — 下限指定にしていたらCIだけが勝手に新版を入れて、無関係な変更で落ちた。

## ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| [tools.md](docs/tools.md) | コマンド・スクリプト一覧、設定、運用、トラブルシューティング |
| [strategies.md](docs/strategies.md) | 同梱戦略、新しい戦略の書き方、**採用までの道のり** |
| [backtesting.md](docs/backtesting.md) | 検証の進め方と、**自分を騙さないための7項目** |
| [hypotheses.md](docs/hypotheses.md) | **仮説の事前登録と結果**（外れたものも全部） |
| [forward-test.md](docs/forward-test.md) | 実施中の前向き検証と判定基準 |
| [symbols.md](docs/symbols.md) | 銘柄ごとの実測スプレッド・最小数量・検証結果 |
| [bingx-plan.md](docs/bingx-plan.md) | 実弾に至るまでの段取り |

## 免責

**このソフトウェアは投資助言ではない。** 同梱の戦略に優位性があるという主張はしていない。むしろ検証の結果、ほとんどが優位性を持たないことを示している。

自動売買は資金を失う可能性がある。実弾を入れる前に [docs/bingx-plan.md](docs/bingx-plan.md) の段取りを読み、**失っても困らない金額から**始めること。口座に置く金額そのものを絞ることが、設定ファイルより確実な防御である。

## ライセンス

MIT
