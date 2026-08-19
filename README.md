# シフト表LINEリマインドBot

シフト表のExcelファイル(.xlsx)をLINEで送ると、自分の出勤日を抽出して登録するBotです。AIは使わず、Excelのセル構造をルールベースで解析します。

- 各出勤日の**前日13:00(JST)**にリマインド(出勤日なら時刻・役割つき、休みの日は「休みです」)
- 自分の出勤時刻の**10分前**に、その日の出勤者リストをプッシュ通知(cron-job.orgのAPIでジョブのスケジュールをその都度書き換えることで、ポーリングなしでピンポイントの時刻に実行)
- 「今日/明日出勤リスト」「今日/明日の自分は出勤?」のQuick Replyボタンでいつでも確認可能
- Excelファイルを送ると、LINE未登録の人も含めて**ファイル内の全従業員分**のシフトを名簿として保存するので、出勤リストには全員分が表示される(個人へのリマインドはLINEで名前登録した本人にのみ届く)
- 月をまたいで新しいファイルを送っても安全:新しいファイルに載っている日付だけを反映し、載っていない日付(まだ来ていない別の月の予定など)は既存データを保持する。今日より前の日付は自動的に削除される(`app/storage.py` の `_merge_shift_data`)

## 使い方(利用者側)

1. LINE公式アカウントを友だち追加する
2. 最初のメッセージでシフト表に載っている自分の表記(名前)を送る(例:`田中`)
3. シフト表のExcelファイル(.xlsx)を送る → その名前の出勤日を抽出して登録
   - LINEでファイルを送る場合は「+」メニューの「ファイル」から選択してください
   - マクロ有効ファイル(.xlsm)はLINEアプリ側の制限で送信できません。Excelで「名前を付けて保存」から `.xlsx` 形式で保存し直してから送ってください
4. 各出勤日の前日13:00にリマインドが届く。休みの日は「休みです」という通知が届く
5. 出勤日は、出勤時刻の10分前に「その日の出勤者リスト」もプッシュ通知で届く
6. 返信メッセージのQuick Replyボタン(「明日出勤リスト」「明日の自分は出勤?」)からいつでも確認できる
7. 名前を間違えた場合は `名前変更 正しい名前` と送る

## 対応しているExcelの表フォーマット

以下の構造を前提にルールベースで解析します(月間のシフト表でよくある形式です)。

- どこかの行に **日付が横に並んだヘッダー行** があること(実際の日付セルでも、1〜31の数値でもどちらでも可)
- 氏名は複数行(通常6行)にまたがる結合セルになっていること。その結合ブロックの1行目に、日付ごとのシフト記号(文字)が入っていること
- シフト記号が「休み記号セット」(`○` `◎` `休` `-` `ー` `off` `OFF` `×` `公休` `希望休` `有給` `欠勤` `空欄`)に一致しなければ出勤日と判定([app/excel_extract.py:11](app/excel_extract.py:11)で定義。会社によって記号が異なる場合はここを編集してください)
- 出勤時刻・退勤時刻は、シフト記号の行から2行下・3行下にあるセルの時刻を読み取る([app/excel_extract.py:19-20](app/excel_extract.py:19)の `_START_TIME_ROW_OFFSET` / `_END_TIME_ROW_OFFSET`。テンプレートが異なる場合はここを調整してください)。取得できない場合は時刻なしで日付のみ通知します
- 年月はシート内の「2026年 8月」のような表記から自動取得(見つからない場合は現在の年月を使用)

この形式に当てはまらない表の場合はうまく抽出できません。その際はBotからの返信メッセージに理由(該当行が見つからない、名前が一致しないなど)が表示されるので、ファイルまたは登録名を見直してください。

## 構成

- `app/main.py` … FastAPI本体。LINE Webhook受信、名前登録、Excel解析結果の登録、リマインド送信エンドポイント
- `app/excel_extract.py` … openpyxlでExcelのセル構造を解析し、該当者の出勤日をルールベースで抽出
- `app/storage.py` … 利用者ごとの名前・出勤日をJSONファイル(`data/users.json`)に保存する簡易ストレージ
- `app/cronjob_client.py` … cron-job.orgのAPIを呼び、出勤アラート用ジョブのスケジュールを動的に書き換える
- `.github/workflows/daily-reminder.yml` … `/internal/send-reminders` を叩くGitHub Actions。自動実行(schedule)は無効化済みで、手動実行(workflow_dispatch)のみ残している
- `render.yaml` … Renderへのデプロイ設定

`/internal/send-reminders`(日次13:00リマインド)は、**GitHub Actionsのscheduleではなくcron-job.org**(外部の無料cronサービス)から直接POSTする運用にしています。GitHub Actionsのスケジュール実行は負荷状況によって大幅に遅延することがある(公式に明記されている既知の制約)ため、時刻精度が必要な部分はcron-job.orgに統一しています。

`/internal/send-shift-start-alerts`(出勤10分前通知)は、数分おきのポーリングではなく、**cron-job.orgのAPI経由でジョブのスケジュールをその都度「次に必要な1回」に書き換える**方式にしています。Excelファイルを受け取った直後と、毎日13:00のリマインド送信時に、翌日の出勤者の中で最も早い出勤時刻を調べ、その10分前ちょうどに1回だけ実行されるようジョブを更新します(`app/main.py` の `_schedule_next_shift_alert_for_date`)。

## セットアップ手順

### 1. LINE Developersでチャネルを作成

1. https://developers.line.biz/ja/ にアクセスし、LINEアカウントでログイン
2. 「プロバイダー」を新規作成(任意の名前)
3. プロバイダー内で「Messaging API」チャネルを新規作成(チャネル名・説明・業種などを入力)
4. 作成したチャネルの管理画面で以下を取得:
   - **チャネルアクセストークン**: 「Messaging API設定」タブ →「チャネルアクセストークン(長期)」を発行
   - **チャネルシークレット**: 「チャネル基本設定」タブに記載
5. 「Messaging API設定」タブで以下を設定:
   - Webhookの利用: オン
   - 応答メッセージ: オフ(Bot側で応答するため)
   - あいさつメッセージ: 任意(オフでも可、Follow時にBotから案内を送るため)
6. LINE Official Account Manager (https://manager.line.biz/) の「設定」→「応答設定」でも、応答モードを「チャットボット」にし、応答メッセージをオフ・Webhookをオンにしておく

### 2. コードをGitHubにpush

このディレクトリの内容をGitHubリポジトリにpushしてください。

```bash
git init
git add .
git commit -m "Initial commit: shift reminder LINE bot"
```

`.env` はコミットしないでください(`.gitignore` 済み)。

### 3. Renderにデプロイ

1. https://render.com/ にログイン(GitHub連携)
2. 「New +」→「Blueprint」からこのリポジトリを選択すると `render.yaml` の内容が反映されます
   (Blueprintを使わない場合は「Web Service」を手動作成し、Build Command: `pip install -r requirements.txt`、Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`)
3. 環境変数を設定:
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_CHANNEL_SECRET`
   - `REMINDER_TRIGGER_TOKEN`(任意のランダム文字列)
   - `CRONJOB_API_KEY`(cron-job.orgのAPIキー。手順5参照)
   - `CRONJOB_ALERT_JOB_ID`(出勤アラート用ジョブのID。手順5参照)
4. デプロイ完了後に発行されるURL(例: `https://shift-line-bot.onrender.com`)を控える

**注意(無料プランの制限)**: Renderの無料Web Serviceは一定時間アクセスがないとスリープします。スリープ中にLINEからWebhookが来ると応答が遅れる/失敗することがあります。安定運用したい場合は有料プラン(最小構成で月$7程度)への切り替えを推奨します。またRenderの無料プランはディスクが永続化されないため、再デプロイのたびに `data/users.json` の登録内容が消える点にも注意してください(継続利用するなら後述の「発展」を検討)。

### 4. LINE DevelopersにWebhook URLを設定

「Messaging API設定」タブの Webhook URL に `https://<Renderのドメイン>/webhook` を設定し、「検証」で成功することを確認。

### 5. cron-job.orgでリマインド送信をスケジュール

1. https://cron-job.org/ で無料アカウントを作成
2. 日次リマインド用ジョブを作成:
   - URL: `https://<Renderのドメイン>/internal/send-reminders?token=<REMINDER_TRIGGER_TOKENの値>`
   - Schedule: 毎日13:00(JSTタイムゾーンを指定するか、UTC 04:00で設定)
   - Request method: POST
3. 出勤アラート用ジョブを作成(スケジュールは仮でよい。Botが毎回書き換えます):
   - URL: `https://<Renderのドメイン>/internal/send-shift-start-alerts?token=<REMINDER_TRIGGER_TOKENの値>`
   - Request method: POST
   - 保存後、ジョブ詳細画面のURLに含まれる数字が Job ID。これを `CRONJOB_ALERT_JOB_ID` としてRenderに設定
4. APIキーを発行: cron-job.orgの「Settings」→「API」タブ →「Create API Key」。これを `CRONJOB_API_KEY` としてRenderに設定
5. (任意)Renderのスリープ防止用に、`https://<Renderのドメイン>/health` を10分おきに叩くジョブも作成しておくと安定します(トークン不要)
6. 過去データ削除用ジョブを作成:
   - URL: `https://<Renderのドメイン>/internal/prune-past-dates?token=<REMINDER_TRIGGER_TOKENの値>`
   - Schedule: 毎日00:05(JST。日付が変わった直後)
   - Request method: POST
   - 今日より前の日付のシフト・休みデータを削除する(今日分は`>= 今日`の判定で保持されるので、日中に消えることはない)

GitHub Actionsの `daily-reminder.yml` はバックアップ用に残していますが、自動実行(schedule)は無効化しているので、普段は使いません。何かの理由でcron-job.orgを使わず手動できっかけを作りたい場合のみ、Actionsタブの「Run workflow」から実行できます。

## ローカルでの動作確認

```bash
pip install -r requirements.txt
cp .env.example .env  # 値を埋める
uvicorn app.main:app --reload
```

外部(LINE)からWebhookを受けるには ngrok 等でトンネルを張り、そのURLをLINE DevelopersのWebhook URLに設定してください。

リマインド送信を手動テストする場合:

```bash
curl -X POST "http://localhost:8000/internal/send-reminders?token=<REMINDER_TRIGGER_TOKEN>"
```

## 発展(必要であれば)

- 複数名分のシフトが1枚の表にある場合でも、登録名でセルを判別する方式なので同じ表を複数人が送っても個別に自分の分だけ登録される
- 表のフォーマットが対応形式と異なる場合は `app/excel_extract.py` の `_find_day_header` / `_find_name_and_block` / `_extract_shifts_for_name` の判定ロジックや、休み記号セット(`_OFF_MARKERS`)、役割の正式名称表(`app/main.py` の `_ROLE_NAMES`)を実際のファイルに合わせて調整する
- データ永続化を強化したい場合はJSONファイルの代わりにRenderの有料プランでディスクを永続化するか、外部DB(Supabase等)に切り替え可能
- リマインド時刻を変えたい場合は `.github/workflows/daily-reminder.yml` の cron 式を変更(UTC基準なのでJSTから9時間引いた時刻を指定)
- 出勤10分前通知のチェック間隔を変えたい場合は cron-job.org 側のジョブ設定(Schedule)を変更
