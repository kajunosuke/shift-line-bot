# シフト表LINEリマインドBot

シフト表の写真・Excelファイル(.xlsx)・PDFファイルをLINEで送ると、自分の出勤日を抽出して登録し、各出勤日の**前日13:00(JST)**にリマインドを送るBotです。

## 使い方(利用者側)

1. LINE公式アカウントを友だち追加する
2. 最初のメッセージでシフト表に載っている自分の表記(名前)を送る(例:`田中`)
3. シフト表の写真、Excelファイル(.xlsx / .xlsm)、またはPDFファイル(.pdf)を送る → その名前の出勤日を抽出して登録
   - Excel・PDFファイルは画像より認識精度が高いので、写真がぼやけて認識できない場合はファイルでの送信がおすすめです
   - LINEでファイルを送る場合は「+」メニューの「ファイル」から選択してください(写真として送ると画像扱いになります)
4. 各出勤日の前日13:00にリマインドが届く
5. 名前を間違えた場合は `名前変更 正しい名前` と送る

## 構成

- `app/main.py` … FastAPI本体。LINE Webhook受信、名前登録、画像解析結果の登録、リマインド送信エンドポイント
- `app/vision.py` … Claude(Anthropic API)にシフト表画像を渡して該当者の出勤日をJSONで抽出
- `app/storage.py` … 利用者ごとの名前・出勤日をJSONファイル(`data/users.json`)に保存する簡易ストレージ
- `.github/workflows/daily-reminder.yml` … 毎日13:00 JSTに `/internal/send-reminders` を叩くGitHub Actions
- `render.yaml` … Renderへのデプロイ設定

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

### 2. Anthropic APIキーを用意

すでにお持ちのAPIキー(https://console.anthropic.com/ で発行)を使用します。

### 3. コードをGitHubにpush

このディレクトリの内容をGitHubリポジトリにpushしてください。

```bash
git init
git add .
git commit -m "Initial commit: shift reminder LINE bot"
```

`.env` はコミットしないでください(`.gitignore` 済み)。

### 4. Renderにデプロイ

1. https://render.com/ にログイン(GitHub連携)
2. 「New +」→「Blueprint」からこのリポジトリを選択すると `render.yaml` の内容が反映されます
   (Blueprintを使わない場合は「Web Service」を手動作成し、Build Command: `pip install -r requirements.txt`、Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`)
3. 環境変数を設定:
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_CHANNEL_SECRET`
   - `ANTHROPIC_API_KEY`
   - `REMINDER_TRIGGER_TOKEN`(任意のランダム文字列。GitHub Actions側と一致させる)
4. デプロイ完了後に発行されるURL(例: `https://shift-line-bot.onrender.com`)を控える

**注意(無料プランの制限)**: Renderの無料Web Serviceは一定時間アクセスがないとスリープします。スリープ中にLINEからWebhookが来ると応答が遅れる/失敗することがあります。安定運用したい場合は有料プラン(最小構成で月$7程度)への切り替えを推奨します。またRenderの無料プランはディスクが永続化されないため、再デプロイのたびに `data/users.json` の登録内容が消える点にも注意してください(継続利用するなら後述の「発展」を検討)。

### 5. LINE DevelopersにWebhook URLを設定

「Messaging API設定」タブの Webhook URL に `https://<Renderのドメイン>/webhook` を設定し、「検証」で成功することを確認。

### 6. GitHub Actionsでリマインド送信をスケジュール

リポジトリの Settings → Secrets and variables → Actions で以下を登録:

- `REMINDER_ENDPOINT_URL` = `https://<Renderのドメイン>/internal/send-reminders`
- `REMINDER_TRIGGER_TOKEN` = Renderに設定したものと同じ値

`.github/workflows/daily-reminder.yml` が毎日04:00 UTC(=13:00 JST)に自動実行され、翌日が出勤日の利用者にリマインドをpushします。手動実行は Actions タブの「Run workflow」から可能です。

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
- データ永続化を強化したい場合はJSONファイルの代わりにRenderの有料プランでディスクを永続化するか、外部DB(Supabase等)に切り替え可能
- リマインド時刻を変えたい場合は `.github/workflows/daily-reminder.yml` の cron 式を変更(UTC基準なのでJSTから9時間引いた時刻を指定)
