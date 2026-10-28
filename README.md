# Codex Session Analysis

Codexの実行履歴（Rollout JSONL）から、トークン消費・実行時間・ツール出力・失敗・重複を調べ、開発効率の改善案をまとめる[Agent Skill](https://agentskills.io/)です。

Pythonでログを集計し、Codexには必要な結果だけを渡します。**ログ全文をモデルに読み込ませる必要はありません。**

## インストール

Python 3.10以上とCodex CLIが必要です。Pythonの追加パッケージは不要です。

Windows PowerShell（どのリポジトリでも使用する場合）：

```powershell
New-Item -ItemType Directory -Force "$HOME/.agents/skills" | Out-Null
git clone https://github.com/YutoMatsui21/codex-session-analysis.git "$HOME/.agents/skills/codex-session-analysis"
```

特定のリポジトリでだけ使う場合は、そのリポジトリのルートで次を実行します。

```powershell
New-Item -ItemType Directory -Force ".agents/skills" | Out-Null
git clone https://github.com/YutoMatsui21/codex-session-analysis.git ".agents/skills/codex-session-analysis"
```

すでに同名のSkillがある場合は、重複配置せず、既存版の扱いを確認してください。CodexがSkillを認識しない場合は再起動します。

## 使い方

Codexに次のように依頼します。

> $codex-session-analysis を使って直前の開発セッションを分析し、品質を維持したまま削減できるトークン消費と待機時間を調べてください。

PRなど対象が分かっている場合：

> PR #123を作成したセッションを分析し、根拠付きで改善案を優先順位順に報告してください。分析だけ行い、ファイルは変更しないでください。

Skillはログの特定、集計、必要なツール結果の確認、改善案の検討を順に実施します。

### 集計コマンドを直接使う場合

以下はユーザー全体にインストールした場合のPowerShell例です。

```powershell
$script = "$HOME/.agents/skills/codex-session-analysis/scripts/analyze_session.py"

# 直近のセッション
python -B $script overview --last 5 --project .

# 対象ログを指定して集計
python -B $script report --file "<JSONLのパス>" --top 5 --max-total-chars 6000

# 必要な箇所だけ取得
python -B $script calls --file "<JSONLのパス>" --ids 1,3,7
python -B $script detail --file "<JSONLのパス>" --id 7 --around 0 --max-chars 1200 --max-total-chars 2500
python -B $script message --file "<JSONLのパス>" --line 255 --max-chars 1500
```

ログの探索先は通常`~/.codex/sessions`です。`CODEX_HOME`を設定している場合は、その配下の`sessions`を参照します。特定のリポジトリには依存しません。

## コマンド

| コマンド | 用途 |
|---|---|
| `overview` | セッションの概要・トークン・実行時間・ツール呼び出し |
| `report` | 大量出力、長時間処理、重複・失敗などの集計 |
| `calls` | ツール呼び出しをIDや範囲で抽出 |
| `detail` | 指定したツール呼び出しと周辺の情報 |
| `message` | 行番号を指定してユーザー・アシスタントの文章を抽出 |

`--help`で各コマンドのオプションを確認できます。

## 計測例

3件の開発セッションを分析した際、元のJSONL全文は`o200k_base`換算で合計5,388,928トークン、分析セッションのツール出力は28,982トークンでした。**全文との情報量比較では99.46%減**です。

これは1回の計測例であり、**APIで消費したトークン数・料金の削減率ではありません**。分析セッション全体の消費量は別途計測する必要があります。

## 注意事項

- JSONLにはコード、入力内容、認証情報などが含まれることがあります。実際のログは公開リポジトリにアップロードしないでください。
- Codexのログ形式に依存するため、将来の形式変更によって一部の情報を読み取れなくなる可能性があります。
- スクリプトは読み取り専用です。分析対象のJSONLを書き換えません。

## ライセンス

[MIT License](LICENSE)
