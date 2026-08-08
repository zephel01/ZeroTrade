# ZeroTrade

ドル円を中心としたFX・株式の自動売買を、**ルール徹底とリスク管理を強制した形で**実行するベースシステム。

監視時間が取れないこと、そして裁量でルールを破ってしまうことを、システム側の構造で防ぐことを目的にしている。

## 設計の芯

このシステムには4つの譲れない前提がある。

戦略はシグナルを出すことしかできない。ロットを決めることも、注文を出すこともできない。「今日は調子がいいからロットを上げる」という判断がコードレベルで不可能になっている。

すべての新規注文は `RiskManager` を必ず通る。`OrderManager.submit()` がリスク判定を経由しないパスを持たないため、リスクチェックを迂回した発注は書けない。

日次・週次の損失上限に達すると自動で停止する。停止状態は JSON に永続化されるので、プロセスを再起動しても上限がリセットされない。停止中でも決済注文（`reduce_only`）だけは通る。建玉を閉じられない停止は、リスク管理として本末転倒だからである。

ブローカー固有の仕様は Adapter の内側に閉じ込める。OANDA の符号付き `units` も、日本株の単元株制も、コア層には漏れない。

## クイックスタート

APIキーが無くてもそのまま動く。`PaperBroker` が疑似価格を生成する。

```bash
pip install -e ".[dev,ui]"

zerotrade -c config/paper.yaml check                  # 設定検証（発注しない）
zerotrade -c config/paper.yaml run --iterations 300   # ペーパートレードを300ループ
zerotrade -c config/paper.yaml dashboard              # TUI で監視（別ターミナル）
zerotrade -c config/paper.yaml report                 # HTMLレポートを書き出す
```

OANDA に実接続する場合は環境変数を設定する。

```bash
export OANDA_ACCOUNT_ID="101-xxx-xxxxxxx-xxx"
export OANDA_API_TOKEN="..."
zerotrade -c config/oanda.yaml check
```

`config/oanda.yaml` は `mode: paper` で始まる。実弾を入れる準備ができたら `mode: live` に変え、`fallback_to_paper: false` にすること。

**`mode` は表示用のラベルではなく、実弾を止める仕掛けである。** `mode: paper` のまま実在の取引所を `environment: live` で指定した構成は、起動時に拒否される。逆に `mode: live` かつ `broker.name: paper` も拒否される。どちらも「設定に書いてあることと実際に起きることが食い違う」組み合わせだからである。

## バックテスト

**エンジンは本番とまったく同じ経路を通す。** 戦略もサイズ決定もリスク検査も発注も、ライブ実行と同じ `StrategyRunner` / `RiskManager` / `OrderManager` を通る。バックテスト専用のロジックを別に書くと、そこに紛れ込んだ差異が「検証では勝てたのに実弾では負ける」の温床になるからである。ライブとの違いは、時計が相場時間になること、ループ間の待機が無いこと、通知を出さずリスク状態をディスクに残さないことの3点だけに絞ってある。

時計を相場時間にしているのは飾りではない。実時間のままだと2年ぶんを数秒で流したときに日付が一度も変わらず、日次損失上限がリセットされないまま最初の停止で終わってしまう。

```bash
# 公開ソースから取得する（口座不要）
zerotrade download --provider yahoo --symbol USD_JPY --granularity H1 --days 720

# 外部で入手した OHLCV を取り込み、足種を揃える
zerotrade import --csv DAT_ASCII_USDJPY_M1_2024.csv --resample M5 -o data/USDJPY_M5.csv

# ブローカーAPIが使えるなら直接取得もできる
zerotrade fetch --symbol USD_JPY --granularity M5 --days 365

# 検証（--report でHTMLも出せる）
zerotrade backtest --csv data/USDJPY_M5.csv --report state/backtest.html

# パラメータ掃引
zerotrade optimize --csv data/USDJPY_M5.csv --param fast_period=10,20,30 --param atr_stop_multiplier=1.5,2,3
```

`download` は Yahoo Finance と Stooq から認証なしで取得する。ただしどちらも非公式・無保証のエンドポイントなので、戦略のふるい分け用と考えること。`import` は HistData（`YYYYMMDD HHMMSS;O;H;L;C;V` のセミコロン区切り）、MetaTrader 4/5 のエクスポート（`YYYY.MM.DD,HH:MM,...`）、Dukascopy、汎用のヘッダ付きCSVを自動判別し、1分足から任意の足種へまとめ直す。時刻はすべて UTC に正規化する。HistData は夏時間なしの米国東部時間、MT4 は EET（冬 UTC+2 / 夏 UTC+3）で配布されているので、UTC と誤解すると足がずれたまま検証が走ることになる。MT4 のオフセットは実データの週末ギャップ位置（金曜17時ニューヨーク時間のクローズ）から実測して確定した。業者によって違うので、ずれていれば `--tz` で明示すること。

結果は本番とは別の `state/backtests/` 以下に書き出され、`zerotrade report` と同じレポートで読める。実運用の成績と混ざらないようにしてある。

### スプレッドは検証結果を支配する

`broker.spread` は**必ず実際に使うブローカーの実勢に合わせること**。USD/JPY の実データ（2024年・5分足・1247トレード）で測ると、こうなった。

| スプレッド | 損益 | プロフィットファクタ |
|-----------|------|-------------------|
| 0銭 | +12,615 | 1.00 |
| 0.3銭（OANDA実勢） | -84,183 | 0.97 |
| 0.8銭 | -225,259 | 0.92 |
| 2銭 | -479,227 | 0.81 |

同じ戦略・同じデータで、設定値ひとつで **-8% から -48% まで6倍の差**が出る。既定値は 0.3銭にしてあるが、これを実勢とずらしたまま検証すると結果そのものが意味を失う。

なおこの表は同時に、同梱戦略が**取引コストを払えるだけのエッジを持っていない**ことも示している。コストゼロでプロフィットファクタがちょうど 1.00 ということは、素の勝ち負けが完全に五分ということである。

### 過学習について

`optimize` は探索と評価を最初から分けている。足を前半（in-sample）と後半（out-of-sample）に割り、**探索は前半だけで行い、選んだパラメータを後半で答え合わせする**。両方の成績を並べて出すので、前半だけ突出して後半で崩れる組み合わせは ○/× で一目で分かる。

順位付けは素の損益ではなく、純損益をドローダウンで割った値を既定にしている。「最終的にいくら増えたか」だけを見ると、途中で口座の半分を溶かす経路も高く評価されてしまうからである。トレード数が5件未満の組み合わせは統計として意味を持たないので強く減点する。

## 監視と振り返り

画面は取引プロセスとは**別プロセス**で動き、SQLite の記録を読むだけである。表示側が落ちても取引は続くし、bot を止めていても履歴は見られる。Web サーバーは立てないので、ポートを開ける必要も認証を用意する必要もない。

`zerotrade dashboard` はターミナルに常駐する監視画面。口座残高・確定損益・勝率・停止状態に加えて、直近のトレード、却下されたリスクルールの内訳、運用の節目を2秒ごとに更新する。SSH 越しでも動く。

`zerotrade report` は equity カーブとトレード履歴を1枚の HTML に書き出す。JavaScript も CDN も使わない自己完結の HTML なので、ブラウザで開くだけで見られるし、Discord に添付しても他のマシンにコピーしても壊れない。`--days 7` で期間を絞れる。

**画面からできる操作は緊急停止だけ**にしてある。これは意図的な制約である。スマホから「再開」を押せる画面を作った瞬間、損失上限で止めた意味がなくなる。停止方向にしか作用しない `state/STOP` ファイルの作成だけを表示層に許し、再開（`zerotrade resume`）と手動決済は CLI に残して摩擦を保っている。ダッシュボードでは誤爆を防ぐため `s` を2回押して確定する。

```bash
zerotrade -c config/paper.yaml stop            # CLI からの緊急停止
zerotrade -c config/paper.yaml status          # 現在のリスク状態
zerotrade -c config/paper.yaml resume          # 停止の解除（意図的に CLI のみ）
```

記録層に残るのは、決済済みトレード、発注、リスク検査で却下された注文とそのルール、HOLD 以外のシグナル、equity のスナップショット、そして起動・停止・損失上限到達といった節目である。とくに**却下されたルールの内訳**は、「フィルタが厳しすぎるのか戦略自体が弱いのか」を切り分ける唯一の一次情報になる。

## 構成

```
src/zerotrade/
├── models.py          # Order / Position / Balance / Signal などのドメインモデル
├── settings.py        # YAML + 環境変数の設定（pydantic で検証）
├── errors.py          # 例外階層
├── control.py         # キルスイッチ（停止方向にのみ作用）
├── report.py          # 静的HTMLレポート生成
├── tui.py             # TUI ダッシュボード（Textual・任意依存）
├── app.py             # 依存関係の配線
├── cli.py             # zerotrade コマンド
├── core/
│   ├── risk.py        # RiskManager ★中核
│   ├── sizing.py      # PositionSizer
│   ├── orders.py      # OrderManager（発注の唯一の入口）
│   ├── runner.py      # StrategyRunner（実行ループ）
│   └── notifier.py    # Discord / Slack / コンソール
├── brokers/
│   ├── base.py        # BaseBroker 抽象インターフェース
│   ├── paper.py       # 約定シミュレータ
│   ├── oanda.py       # OANDA v20 REST
│   ├── ccxt_broker.py # ccxt 経由で100以上の仮想通貨取引所
│   ├── bingx.py       # BingX 専用（片方向モード・確定損益）
│   └── shadow.py      # 実勢価格で読み、約定は手元で模擬（前向き検証用）
├── data/
│   ├── feed.py        # MarketDataFeed 抽象
│   ├── fetcher.py     # ブローカーAPIからの分割取得
│   ├── importer.py    # 外部CSVの取り込みとリサンプリング
│   └── historical.py  # CSV読込・疑似データ生成
├── store/
│   ├── sqlite.py      # 記録層（WAL・別プロセスから読める）
│   └── models.py      # 読み取り用の行モデルと成績集計
├── strategies/
│   ├── base.py        # Strategy 抽象 + プラグインレジストリ
│   ├── indicators.py  # SMA / EMA / RSI / ATR（純Python・Decimal）
│   └── sma_rsi.py     # SMAクロス + RSIフィルタ + ATRストップ
└── backtest/
    ├── engine.py      # 本番と同じ経路を早送りする
    └── optimize.py    # パラメータ掃引（in/out-of-sample 分割つき）

scripts/               # 汎用でない作業（いずれも発注しない）
├── forward_start.sh   # 前向き検証をまとめて起動
├── forward_status.sh  # 生存確認
├── forward_watch.py   # 建玉・損益・件数を1画面で表示
├── forward_judge.py   # 進捗と合否判定
├── fetch_*.py         # 検証用データの取得（認証不要）
├── survey_symbols.py  # 複数銘柄の横並び検証
└── hypothesis_*.py    # 事前登録した仮説の検定（再現用）
```

金額・数量・価格はすべて `Decimal` で扱う。float の丸め誤差が「口座の1%」という制約に混入するのを避けるためで、float を使うのは指標計算の内部だけに限定している。

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

判定結果は `RiskDecision` として返り、却下されたルール名（`max_risk_per_trade` など）が機械的に取り出せる。ログ・通知・テストのすべてがこのルール名をキーに動く。

日次・週次のリセット基準は `reset_timezone` で指定する。既定の設定ファイルでは `Asia/Tokyo` にしてあるので、日本時間の日付変更でカウンタがリセットされる。

## 運用フロー

戦略がシグナルを生成し、`PositionSizer` がストップまでの距離からロットを逆算する。`RiskManager` が全ルールを検査し、通過した場合のみ `OrderManager` がブローカーへ委譲する。約定後は建玉を監視し、ストップ・利確・強制決済をルールに従って実行して、日次でサマリを通知する。

`StrategyRunner` にはブローカー側のストップが機能しなかった場合の保険として、現在値がストップ価格を明確に超えていれば強制決済する経路を持たせてある。

停止時に建玉は自動決済しない。一時的なネットワーク断で再起動しただけで意図しない損失確定が起きるのを避けるためで、建玉の扱いは運用者の判断に委ねている。

## 新しいブローカーを足す

`BaseBroker` の抽象メソッド8個（`connect` / `disconnect` / `get_balance` / `get_positions` / `place_order` / `cancel_order` / `get_order` / `get_open_orders` / `get_ticker`）を実装し、`register_broker()` で登録して、設定の `broker.name` にその名前を書く。コア層のコードは一切変更しなくてよい。

正確な確定損益を返せるブローカーは `get_closed_trades()` を実装して `supports_closed_trades = True` にする。日次・週次の損失上限はこの確定損益を基準にしている。返せない場合は `StrategyRunner` が建玉の差分から推定するので、損失上限は働き続ける。

仮想通貨取引所は `ccxt` アダプタで一括対応している（`pip install "zerotrade[ccxt]"`）。ccxt の統一APIが `BaseBroker` とほぼ1対1で対応するため、1ファイルで100以上の取引所が使える。テストネットに対応した取引所なら、**入金ゼロでライブ発注の経路を通しで検証できる**。

BingX は専用アダプタ（`broker.name: bingx`）を用意してある。汎用アダプタでは対応しきれない4点 — 建玉照会に銘柄リストが要る・ポジションモードを片方向へ揃える必要がある・`clientOrderId` の照会期限が2時間・`fetchPositionsHistory` で実現損益が取れる — を作り込んでいる。VST（デモ環境）が使えるので、`environment: practice` で発注から決済まで一周させられる。詳細は [docs/tools.md](docs/tools.md) を参照。

## 検証結果 — 「勝てる戦略」は見つかっていない

独立した2期間の実データで検証した結果を、都合の悪い部分も含めて残しておく。

**データ**: 2024年（HistData・37万本の1分足）を開発用、2025年8月〜2026年6月（MT4エクスポート・34万本）を検定用とした。取得元も期間も重ならない。スプレッドは実勢の0.3銭、1時間足。

| 戦略 | 2024（開発用） | 2025-2026（検定用） |
|------|---------------|-------------------|
| 単純保有 | +9.7% | **+10.2%** |
| `sma_rsi` | -14.8%（PF 0.70） | -1.1%（PF 0.98） |
| `donchian` 既定 | **+18.8%**（PF 1.37） | -7.4%（PF 0.87） |
| `donchian` ロングのみ | +18.6%（PF 1.64） | -2.1%（PF 0.94） |

`donchian` は2024年で単純保有の倍のリターンを出したが、**検定用データでは全変種が負け、どれも単純保有に届かなかった**。しかもこの +18.8% はパラメータ調整の結果ではなく、教科書的な既定値（20/10・ATR3倍）のままの数字である。調整していないのに再現しない、というのが実態だった。

原因もデータで確認できる。ドリフトを差し引いた「20時間高値ブレイク後の超過リターン」は、2024年では t=+2.09 だったのが、2025-2026年では **t=-0.06** と完全に消えていた。もともと18通り試したうちの1つが t>2 だっただけで、多重検定を踏まえれば偶然の範囲だったということである。

ここでパラメータを掃引すれば「勝つ数字」は作れるが、それは検定用データを開発用に変えてしまう行為であり、この検証全体が無意味になる。

**現時点で言えること**は次のとおり。この2期間はどちらもドル円の上昇局面で、単純保有が +10% 前後という強いベンチマークになっている。単一通貨ペア・1時間足の順張りでこれを安定して超える証拠は得られていない。判定には最低でも複数年・複数通貨ペア、できれば下落局面やレンジ局面を含む期間が要る。

## 同梱戦略のロジック

### donchian（ブレイクアウト順張り）

N本高値の更新で買い、N本安値の更新で売る。長期移動平均に逆らわず、**利確を置かない**。順張りは少数の大きな勝ちで多数の小さな負けを賄う構造なので、利確を置くとその「少数の大きな勝ち」を自分で切ってしまう。代わりにシャンデリア・エグジット（建玉保有中の高値からATRの倍数を引いた位置）でストップを追随させる。決済は入りより短い期間のブレイクで素早く行う。`min_hour_utc` / `max_hour_utc` で時間帯を絞ることもできる。

トレーリングストップは `SignalAction.UPDATE_STOP` として実装した。**引き上げ方向にしか動かない**。買い建玉のストップを下げる操作は損失許容量を後から広げる行為で、リスク管理の前提が崩れるため、ブローカー側で拒否している。

### sma_rsi（SMAクロス）

`sma_rsi` は、短期SMA(20)が長期SMA(50)を**その足で**上抜け、かつRSI(14)が70未満のときにロングへ入る（ショートは対称、`allow_short: false` で無効化可）。クロスが成立した足でしか入らないので、乗り遅れたトレンドは追いかけない。ストップは `終値 − ATR(14)×2`、利確は `終値 + ATR(14)×3` に置き、このストップ距離から `PositionSizer` がロットを逆算する。決済は逆方向クロスによるシグナル決済、ブローカー側のSL/TP到達、そして StrategyRunner による強制決済の3経路。

これはエッジがあると主張できる戦略ではない。目的は「シグナル→サイズ決定→リスク検査→発注→決済→損益確定」が一周することを確認できる最小の実装である。

実データでの成績は上の「検証結果」節のとおりで、2024年・2025-2026年ともに負けている。5分足では1247トレードでコスト負けし、1時間足ではコストを除いても勝てない。

さらにパラメータ掃引（18通り）を掛けると、in-sample で上位に来た5件は**すべて out-of-sample で負けた**。in-sample では最良で +5.95%・PF 1.75・勝率66%という数字が出るが、後半に当てると -3.02%・PF 0.48 に崩れる。過学習の教科書的な例で、`optimize` の in/out 分割はまさにこれを見せるために入れてある。

## 新しい戦略を足す

```python
from zerotrade.strategies import Strategy, StrategyContext, register_strategy
from zerotrade.models import Signal, SignalAction


@register_strategy
class MyStrategy(Strategy):
    name = "my_strategy"
    warmup_bars = 50

    def generate(self, context: StrategyContext) -> Signal: ...
```

設定の `strategy.name` に `my_strategy` を書けば読み込まれる。

## 開発

```bash
pytest                  # テスト
ruff check . && ruff format --check .
mypy                    # strict モード
pre-commit install      # コミット時の自動チェック
```

CI（GitHub Actions）で Python 3.11 / 3.12 の両方に対して lint・型チェック・テストを回している。

## 現在の実装状況

plan.md の優先実装順のうち、1〜4（プロジェクト構成・`BaseBroker`・`RiskManager`・OANDA Adapter・簡易戦略とペーパートレード）と6（バックテスト基盤）が完了している。加えて「必要になれば後付け」とされていた監視層として、SQLite 記録層・TUI ダッシュボード・静的HTMLレポートを実装した。未実装は5の日本株 Adapter（kabu ステーション / 立花証券）のみ。

なお OANDA証券の REST API はデモ口座では利用できず、本番口座（NYサーバーのプロコース）・会員ステータス Gold・口座残高25万円以上が条件になっている。API が使えるようになるまでは `zerotrade import` で外部のヒストリカルデータを取り込んで検証を進められる。

## ドキュメント

| ドキュメント | 内容 |
|-------------|------|
| [docs/tools.md](docs/tools.md) | コマンド一覧、設定、運用、トラブルシューティング |
| [docs/backtesting.md](docs/backtesting.md) | 検証の進め方と、自分を騙さないための7項目 |
| [docs/strategies.md](docs/strategies.md) | 同梱戦略の中身と、新しい戦略の書き方 |
| [docs/plan.md](docs/plan.md) | 当初の仕様書 |

## ライセンス

MIT
