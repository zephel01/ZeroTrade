# コマンドとツールの使い方

`zerotrade` の全サブコマンドと、日々の運用の流れ。

---

## 準備

```bash
pip install -e ".[dev,ui]"     # ui は TUI ダッシュボード用（任意）
zerotrade --help
```

設定ファイルは `-c` で指定する。省略すると既定値だけで動く。

```bash
zerotrade -c config/paper.yaml <サブコマンド>
```

同梱の設定は5つある。`config/paper.yaml` はペーパートレード用でAPIキー不要、`config/oanda.yaml` はOANDA接続用、`config/ccxt.yaml` は仮想通貨取引所の汎用接続用、`config/bingx.yaml` は BingX 用、`config/pepe-forward.yaml` は前向き検証用（シャドー実行）。いずれも環境変数から認証情報を読む。

秘密情報は YAML に直接書かず `${OANDA_API_TOKEN}` の形で環境変数を参照する。`.env.example` を `.env` にコピーして使うとよい（`.env` は `.gitignore` 済み）。

---

## コマンド一覧

| コマンド | 用途 | 発注するか |
|---------|------|-----------|
| `check` | 設定を検証して構成を表示（`--connect` で疎通確認） | しない |
| `strategies` | 利用可能な戦略を一覧 | しない |
| `download` | 公開ソースからヒストリカル取得 | しない |
| `fetch` | ブローカーAPIからヒストリカル取得 | しない |
| `import` | 手元のCSVを取り込み・足種変換 | しない |
| `backtest` | ヒストリカル上で検証 | しない |
| `optimize` | パラメータ掃引 | しない |
| `report` | HTMLレポートを書き出す | しない |
| `dashboard` | TUIで監視 | しない |
| `status` | リスク状態を表示 | しない |
| `verify` | **発注経路を最小数量で検証** | **する** |
| `run` | **実行ループを開始** | **する** |
| `stop` | 稼働中のループへ緊急停止を要求 | しない |
| `resume` | 損失上限による停止を解除 | しない |

**実際に発注するのは `run` と `verify` だけ**である。他はすべて安全に試せる。`verify` は確認プロンプトを出し、`--dry-run` を付ければ読み取りだけで済む。

---

## まず動かす

```bash
# 設定が正しいか確認する（発注は一切しない）
zerotrade -c config/paper.yaml check

# ペーパートレードを300ループ回す
zerotrade -c config/paper.yaml run --iterations 300
```

`check` は設定値を読める形で並べる。リスク上限、リセット基準タイムゾーン、状態ファイルと記録DBの場所が確認できる。**実弾を入れる前に必ず通すこと。**

```bash
zerotrade -c config/bingx.yaml check --connect
```

`--connect` を付けると**実際にブローカーへ接続し**、残高・建玉・気配値を取得して表示する。叩くのは読み取り系APIだけで、発注は一切しない。APIキーが本当に使えるかは `check` 単体では分からない（設定を読むだけなので）ため、キーを登録したら必ずこれを通すこと。

認証情報が読めずに PaperBroker へ降格していた場合は警告を出して終了コード1で落ちる。これは**気づかないまま「動いている」と誤認する事故**を防ぐためのもので、`--connect` 無しでは検出できない。

`--iterations` を省略すると `Ctrl-C` か `stop` まで回り続ける。

---

## 仮想通貨取引所につなぐ（ccxt）

```bash
pip install "zerotrade[ccxt]"

export CCXT_API_KEY="..."
export CCXT_API_SECRET="..."
zerotrade -c config/ccxt.yaml check --connect
```

ccxt の統一APIは `BaseBroker` とほぼ1対1で対応するため、1つのアダプタで**100以上の取引所**が使える。`broker.exchange` に `binance` / `bybit` / `okx` / `bitflyer` などを書くだけでよい。

### なぜこれを入れたか

**ライブ発注の経路を実物のAPIで検証できるようになる**のが最大の理由である。OANDA アダプタはモックでしか検証できておらず、口座条件（本番口座・プロコース・残高25万円）で塞がっている。仮想通貨取引所のAPIキーは無料で即時に発行でき、多くはテストネットも持つ。`environment: practice` を指定すると `set_sandbox_mode` が有効になり、**入金ゼロで発注から決済までを一周させられる**。

### 安全に始める順番

1. **取引権限を外した読み取り専用キー**で `check --connect` を通す
2. `environment: practice`（テストネット）で `run --iterations 50`
3. 問題なければ本番へ。ただし**出金権限は絶対に付けないこと**

### 取引所ごとに残る差

ccxt が統一するのは**インターフェースであって意味ではない**。次は取引所ごとに違ったまま残る。

| 項目 | 影響 |
|------|------|
| 注文の刻み・最小数量 | 合わせないと注文が弾かれる。`load_markets` から取得して自動で丸める |
| ポジションモード | 一方向／両建てで決済の意味が変わる。ZeroTrade は一方向前提 |
| `reduceOnly` の対応 | 無視する取引所では決済注文が新規建てになる。送る前に建玉を確認している |
| ストップ注文の指定 | **渡し方を間違えると新規建てが弾かれる**（下記）。強制決済が最後の砦 |

### ストップの渡し方には2種類ある

ccxt では意味がまったく違う2つの書き方がある。取り違えると新規建てが通らない。

| 書き方 | 意味 |
|--------|------|
| `stopLossPrice` | **この注文自体がストップ注文である。** ccxt が `reduceOnly` を立てるため、既存の建玉が要る |
| `stopLoss: {triggerPrice: X}` | **新規建てにストップを添付する。** ZeroTrade が使うのはこちら |

前者を新規建てに使うと、BingX は `{"code":109420,"msg":"position not exist"}` で弾く。実際に本番で踏んだ。ccxt のソースにも `# This can be used to set the stop loss and take profit, but the position needs to be opened first` と書いてある。

`verify` は建玉を作った直後に**ストップが取引所側に入ったか**を確認する。入っていなければ「この建玉は無防備です」として不合格にする。取引所側にストップが無い建玉は、プロセスが落ちた瞬間に誰も見ていない状態になる。強制決済は `run` が動いている間しか働かない。

また仮想通貨は24時間365日動き、週末も休場もない。スワップではなく**ファンディングレート**が損益に効く。`risk.reset_timezone` の日次境界は便宜的なものになる。

### 確定損益の扱い

取引所ごとに決済履歴の形式が違い、建玉との対応付けが信頼できないため、`CcxtBroker` は `supports_closed_trades = False` にしている。この場合 `StrategyRunner` が**建玉の差分から確定損益を推定**して `RiskManager` へ渡す。これが無いと日次・週次の損失上限が一切働かない。

推定なので誤差はある。決済価格は検知時点の気配値で代用するため、前回のループから今回までに動いた分だけずれる。正確さが要るなら `get_closed_trades()` を実装して `supports_closed_trades` を True にすること。

---

## BingX につなぐ

汎用 ccxt アダプタではなく、BingX 専用のアダプタ（`broker.name: bingx`）を用意してある。

```bash
pip install "zerotrade[ccxt]"

export BINGX_API_KEY="..."
export BINGX_API_SECRET="..."
zerotrade -c config/bingx.yaml check --connect
```

### 安全に始める順番

**この順番を飛ばさないこと。** 1と2はどちらも実弾が動かない。

1. **取引権限を外した読み取り専用キー**で `check --connect` を通す（残高と気配値が出れば疎通OK）
2. `environment: practice`（VST=デモ）で `run --iterations 50` — 発注から決済まで一周させる
3. 別ターミナルで `dashboard` を開き、記録が入っているか目で確認する
4. 問題なければ `environment: live` へ

**出金権限は絶対に付けないこと。** 取引権限だけで足りる。BingX の API キー設定でIP制限もかけられるので、固定IPで動かすならかけたほうがよい。

### VST（デモ環境）について

`environment: practice` を指定すると `set_sandbox_mode` が有効になり、エンドポイントが `open-api-vst.bingx.com` に向く。BingX の VST は **Virtual Simulated Trading** で、**入金ゼロで発注から決済までを一周させられる**。OANDA のデモ口座と違って API が使えるので、ライブ発注の経路をここで実物のAPIに対して検証できる。

### 銘柄の書き方

設定には `BTC_USDT` と書く。ccxt の統一シンボルへの変換は接続時に自動で行う。

**ここは素朴な文字列変換では足りない。** 永続契約（swap）の統一シンボルは `BTC/USDT` ではなく **`BTC/USDT:USDT`**（決済通貨のサフィックス付き）で、現物の `BTC/USDT` とは別の銘柄として登録されている。単純に `_` を `/` へ直すと現物を掴み、swap 側では「does not have market symbol BTC/USDT」で気配値も証拠金設定も落ちる。そこで `load_markets` の結果に照らし、`market_type` に一致する銘柄を選ぶようにしてある。

存在しない銘柄を書いた場合は**接続時に**エラーになり、同じ基軸通貨の候補を表示する。発注の瞬間に初めて気づくより安全である。

### 汎用アダプタに対して足したもの

BingX 固有の事情が4点あり、それぞれ対応を入れてある。

| 事情 | 放置するとどうなるか | 対応 |
|------|------------------|------|
| `fetchPositions` が銘柄リストを要求する | 建玉が取れず「建玉が無い」と誤認して二重に建てる | 設定の `symbols` を必ず渡す |
| 片方向／ヘッジのポジションモードがある | ヘッジモードだと決済注文が反対側の**新規建て**として通る | 接続時に片方向モードへ寄せ、発注時は `positionSide: BOTH` |
| `clientOrderId` の照会期限が2時間 | 古い注文を冪等キーで追えない | 追跡は取引所側の注文ID（`broker_order_id`）を優先 |
| `fetchPositionsHistory` で実現損益が取れる | （むしろ好都合） | `supports_closed_trades = True`。**損失上限が推定ではなく実測で効く** |

4点目が実務上いちばん効く。汎用 `CcxtBroker` は建玉の差分から確定損益を推定するので、日次・週次の損失上限に誤差が乗る。BingX では取引所が返す実現損益をそのまま `RiskManager` に渡せる。

なお片方向モードの設定は、**既に建玉があると取引所側が拒否する**。その場合は警告だけ出して続行する（発注時の `positionSide` で片方向として振る舞うため致命的ではない）。ログに警告が出ていたら、建玉を空にしてから再起動して揃えておくとよい。

### スプレッドを実測値に入れ直す

`broker.spread` の既定値 0.003 は **USD/JPY 用**である。BTC/USDT にそのまま使うと、実際には存在しないほど有利な条件で検証することになり、バックテストの成績が桁違いに良く出る。

実測は `check --connect` の気配値行でできる。

```
気配値  : BTC_USDT bid=64931.3 ask=64933.5 （スプレッド 2.2）
```

**ただし `environment: practice`（VST）で測った値を設定に入れてはいけない。** VST は本番よりスプレッドが極端に広い。BTC/USDT で比べると VST 2.2 に対し本番 0.2 で、11倍の開きがあった。VST の値で検証すると、実際より不利な条件で判定することになる。

本番の板は認証不要で取れる。銘柄ごとの実測値は [symbols.md](symbols.md) にまとめてある。**銘柄を変えたら必ず測り直すこと。** 価格帯が違えば絶対値も変わる。

USD/JPY での実測では、スプレッドを 0.3銭 から 2銭 に変えただけで同じ戦略の成績が -8% から -48% へ動いた。ここは検証結果を支配する。

### 仮想通貨で回すときのリスク設定

`config/bingx.yaml` の既定値は FX 用より絞ってある。

| キー | bingx.yaml | 理由 |
|------|-----------|------|
| `max_risk_per_trade` | 0.5% | ボラティリティが FX の数倍ある |
| `assumed_leverage` | 3 | 高レバはクリプトでは清算に直結する |
| `reset_timezone` | UTC | 24時間365日動くので、東京時間の日次境界に意味がない |

同梱の `tokyo_fix` 戦略は**仮想通貨では使えない**。あれは東京9:55の仲値という制度に依存しているので、24時間動く市場には対応するものが無い。`donchian` のようなブレイクアウト系から始めて、必ず先にバックテストで検証すること。

---

## 前向き検証（シャドー実行）

```bash
zerotrade -c config/pepe-forward.yaml run
```

`broker.name: shadow` は、**本番の実勢価格を読みながら、約定だけ手元で模擬する**ブローカーである。前向き検証のための器で、`broker.upstream` に価格の取得元を書く。

```yaml
broker:
  name: shadow
  upstream: bingx
  environment: live      # 本番の板を読む。発注はしないので実弾は動かない
  initial_balance: 1000000
```

**発注は一切外へ出ない。** 上流へ委譲するのは読み取り系メソッドだけで、`place_order` は親クラス（手元の模擬）のままである。上流の発注メソッドを呼ぶ経路がコード上に存在しないため、`environment: live` と書いても実弾は動かない。

### なぜテストネットではないのか

テストネットの板は本番と別物のことがある。BingX の VST で実測したところ、1000PEPE のスプレッドは本番 4.5bp に対し **351bp（78倍）** だった。この差は戦略の優位性（1件あたり 5.63bp）を完全に埋め尽くす。同じデータで背景のスプレッドだけ差し替えると +30.63% が −20.20% になる。

**測りたいものより測定誤差が2桁大きい器では、何を測っても意味がない。**

確かめたいことによって環境を使い分ける。

| 確かめたいこと | 環境 | 必要なもの |
|--------------|------|-----------|
| 戦略の優位性 | `shadow` | 価格の質 |
| 発注・約定の経路 | テストネット（VST） | 約定の質 |
| 最終確認 | 本番・最小額 | 両方 |

### シャドー実行で測れないもの

板の厚みを超える数量を出したときの滑り、急変時に指値が届かない事象、取引所の障害。これらは模擬されない。合格しても、次に約定経路の確認と最小額での実弾という段階が要る。

---

## 発注経路を検証する（配管テスト）

```bash
zerotrade -c config/verify.yaml verify --dry-run     # 読み取りだけ
zerotrade -c config/verify.yaml verify               # 最小数量で1往復（**本物の注文**）
```

発注経路はモックとペーパーブローカーでしか通っていない。テストが通るのは「自分が書いた偽物が期待どおり応答する」からであって、**取引所が実際に何を受け取り何を返すかは別の話**である。

過去に実際起きた食い違いを挙げる。いずれもモックでは見つからなかった。

* `reduce_only` を無視する経路があり、決済注文が**新規建てとして通った**（バックテスト結果が +16% から −14% にひっくり返った）
* `fetchPositions` に銘柄リストを渡さないと建玉が取れず、二重に建てる
* 永続契約の統一シンボルは `BTC/USDT` ではなく `BTC/USDT:USDT`
* 足の取得上限が取引所ごとに違い、超えると**1本も返らない**

BTC/USDT なら 0.0001 枚（約6〜7ドル相当）の往復で、コストは数セント。**未知を潰す対価として安い。**

### 確認する項目

接続、残高照会、気配値取得、建玉照会、足の取得、未確定足の判別、新規注文、冪等キーの往復、建玉の反映、**決済注文（reduce_only）**、建玉の解消、確定損益の取得。

### 安全装置

途中で失敗しても**最後に必ず決済を試みる**。決済できなかった場合は合格と報告せず、「建玉が残っています」と明示して終了コード1で落ちる。ストップは買値の80%に置き、検証中に引っかからないようにしてある。

## 監視する

### TUI ダッシュボード

```bash
zerotrade -c config/paper.yaml dashboard
```

取引プロセスとは**別プロセス**で動き、SQLite の記録を読むだけ。表示側が落ちても取引は続くし、bot を止めていても履歴は見られる。SSH 越しでも動く。

| キー | 動作 |
|------|------|
| `q` | 終了 |
| `r` | 再読み込み |
| `s` | 緊急停止（誤爆防止のため2回押しで確定） |

### HTMLレポート

```bash
zerotrade -c config/paper.yaml report                # 全期間
zerotrade -c config/paper.yaml report --days 7       # 直近7日
zerotrade -c config/paper.yaml report -o ~/report.html
```

JavaScript も CDN も使わない自己完結の1枚。ブラウザで開くだけで見られ、Discord に添付しても他のマシンにコピーしても壊れない。

---

## 止める・再開する

```bash
zerotrade -c config/paper.yaml status     # 現在の状態
zerotrade -c config/paper.yaml stop       # 緊急停止を要求
zerotrade -c config/paper.yaml resume     # 停止を解除
```

**画面から実行できるのは緊急停止だけ**にしてある。これは意図的な制約である。スマホから「再開」を押せる画面を作った瞬間、損失上限で止めた意味がなくなる。

停止は `state/STOP` ファイルの有無だけを合図にしている。この方式を選んだのは**停止方向にしか作用しないから**である。ファイルを作れば止まるが、消しても勝手には始まらない。取引ループの起動そのものが明示的な再開の意思表示なので、起動時に古い要求は解除される。

損失上限による停止の解除（`resume`）は CLI にしか置いていない。摩擦を残すことが、止めた意味を保つ。

---

## 通知

`config/*.yaml` の `notifications` に Discord か Slack の Incoming Webhook URL を設定すると、新規・決済・強制決済・損失上限到達・日次サマリが飛ぶ。

```yaml
notifications:
  console: true
  webhook_url: ${DISCORD_WEBHOOK_URL}
  webhook_kind: discord
  min_level: info
```

通知の失敗が取引を止めることはない。送信エラーはログに落として握りつぶす。

---

## 設定の要点

全項目は `config/paper.yaml` のコメントを参照。特に効くものだけ挙げる。

### `broker.spread`

**実際に使うブローカーの実勢に合わせること。** 短期戦略ではこの値が成績を支配する。実測では同じ戦略・同じデータで 0.3銭なら -8%、2銭なら -48% と6倍の差が出た。既定は 0.3銭（OANDA証券の USD/JPY 実勢）。

### `mode` — 実弾を止める最後の設定

| 値 | 意味 |
|----|------|
| `paper` | 実在の取引所へ**本番環境で**注文を送る構成を拒否する |
| `backtest` | 同上 |
| `live` | 実弾を入れる意思表示。これを書かない限り本番発注はできない |

`mode: paper` のまま `environment: live` の実ブローカーを指定すると、起動時にエラーになる。

```
エラー: mode=paper のまま broker=bingx を environment=live で使うことはできません。
        この構成は実在の取引所へ本物の注文を送ります。
        実弾を入れる意図があるなら mode: live と明記してください
```

**設定に paper と書いてあるのに実弾が動くのは、最悪の裏切り方である。** テストネット（`environment: practice`）と擬似ブローカー（`paper` / `shadow`）は実弾が動かないので、この検査の対象外。

### `strategy.granularity`

**戦略が見る足種。バックテストで使ったCSVの足種と必ず揃えること。**

既定は `M5` である。H1のデータで検証した設定をそのままライブに持っていくと、**同じパラメータでもまったく別の戦略になる**。検証結果は何の保証にもならない。

食い違っている場合は `backtest` が警告を出す。

```
WARNING BTC_USDT の足の間隔は 3600 秒ですが、設定の strategy.granularity は M5 (300 秒) です。
        ライブ実行では設定側の足種が使われるため、この検証結果は保証になりません
```

この警告が出たら、設定を直してから検証をやり直すこと。

### `risk.*`

| キー | 既定 | 意味 |
|------|------|------|
| `max_risk_per_trade` | 1% | 1トレードで許容する最大損失 |
| `max_daily_loss` | 3% | 日次でこれに達したら自動停止 |
| `max_weekly_loss` | 6% | 週次でこれに達したら自動停止 |
| `max_margin_usage` | 30% | 証拠金使用率の上限 |
| `max_open_positions` | 3 | 同時保有数 |
| `require_stop_loss` | 有効 | ストップ無しの新規を禁止 |
| `reset_timezone` | `Asia/Tokyo` | 日次・週次カウンタのリセット基準 |

危険な値は設定バリデータが起動時に弾く。`max_risk_per_trade > max_daily_loss` のような矛盾も拒否される。**リスク上限がゼロのまま動き出すより、起動時に落ちるほうが安全**という方針である。

### `store`

記録層。`dashboard` と `report` はこの DB を読む。無効にすると両方とも表示するものが無くなる。

```yaml
store:
  enabled: true
  equity_interval_seconds: 60   # equityスナップショットの間隔
```

---

## ファイルの置き場所

```
state/
├── risk_state.json          # 日次・週次の損益、停止状態（再起動しても引き継ぐ）
├── zerotrade.db             # 記録層（トレード・注文・却下・シグナル・equity）
├── STOP                     # 緊急停止の要求（存在すれば停止）
├── report.html              # 生成したレポート
└── backtests/               # バックテスト結果（本番の記録とは必ず分ける）
```

`risk_state.json` はプロセスを再起動しても損失上限がリセットされないよう永続化している。壊れていた場合は初期化されるが、その旨をエラーログに出す。

---

## トラブルシューティング

**`requires "apiKey" credential` と出る**
環境変数が読めていない。`echo $BINGX_API_KEY` で確認する。認証情報が無い場合は `fallback_to_paper` により PaperBroker へ降格するが、`check --connect` は警告を出して終了コード1で落ちるので気づける。

**`does not have market symbol ...` と出る**
`market_type` と銘柄の組み合わせが噛み合っていない。`swap` なのに現物にしかない銘柄を指定した場合など。エラーメッセージに候補が出る。

**起動直後に「取引は停止中です」と出る**
前回の損失上限に達したまま。`zerotrade status` で理由を確認し、納得できるなら `resume` で解除する。

**注文が一切通らない**
`report` か `dashboard` で「却下されたルール」の内訳を見る。`require_stop_loss` なら戦略がストップを付けていない、`zero_size` ならリスク上限に対してストップが遠すぎて最小単位に届いていない、`max_margin_usage` なら証拠金使用率の上限。

**`dashboard` が起動しない**
`pip install "zerotrade[ui]"` で textual を入れる。

**`limit: This field must be less than or equal to 1440` と出る**
足の取得本数が取引所の上限を超えている。`fetch` はブローカーが申告する上限（`max_ohlcv_count`）に合わせて自動でページ幅を縮めるので、通常は起きない。新しい取引所を足すときは、この属性を実際の上限に設定すること（BingX 1440 / OANDA 5000 / ccxt の既定 1000）。

**バックテストの足数が想定より少ない**
`import` は埋まりきっていない最後の足を捨てる。未確定の足を残すと、実際には存在しなかった高値安値でストップ判定が動くため。

**OANDA に繋がらない**
デモ口座では REST API を使えない。本番口座（NYサーバーのプロコース）・会員ステータス Gold・口座残高25万円以上が条件。API が使えるまでは `import` で外部データを取り込んで検証を進められる。

---

## 開発

```bash
pytest                              # テスト
ruff check . && ruff format --check .
mypy                                # strict モード
pre-commit install                  # コミット時の自動チェック
```

CI（GitHub Actions）で Python 3.11 / 3.12 の両方に対して lint・型チェック・テストを回している。

---

## 関連ドキュメント

- [backtesting.md](backtesting.md) — 検証の進め方と、自分を騙さないための確認項目
- [strategies.md](strategies.md) — 同梱戦略と、新しい戦略の書き方
- [plan.md](plan.md) — 当初の仕様書
