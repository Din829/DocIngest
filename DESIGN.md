# DocIngest 設計ドキュメント

> Version: 0.1 Draft
> Date: 2026-04-02

---

## 1. 概要

汎用ドキュメント前処理エンジン。任意のドキュメントを入力し、RAG と Agentic Search の両方に使えるフォーマットに自動変換する。

**コア思想**: Markdown を唯一の中間フォーマットとし、同一ファイルから RAG（chunk 検索）と Agentic Search（grep/glob）の両方を提供する。

---

## 2. 設計原則

| 原則 | 説明 |
|------|------|
| **Markdown 統一** | 全ドキュメントを Markdown に変換。RAG も Agentic Search も同じファイルを使う |
| **プログラム優先、AI 兜底** | 80% はルールベース（高速・無料）、20% の異常のみ AI 介入 |
| **設定駆動** | 切り分け戦略・モデル・閾値すべて YAML 設定。ハードコードなし |
| **可插拔** | Parser・Chunker・Model Provider すべて差し替え可能 |
| **エラー耐性** | 1 ファイル失敗しても他に影響しない。失敗はログに記録して続行 |
| **キャッシュ活用** | AI 呼び出し結果をキャッシュ。同一入力で二度課金しない |

---

## 3. アーキテクチャ概要

```
入力: 任意ドキュメント (PDF/PPT/Excel/HTML/画像/テキスト...)
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 1: 解析 (Parse)                                        │
│                                                               │
│ Docling (一次解析エンジン)                                     │
│   - AI レイアウト分析 (IBM 版面検出モデル)                      │
│   - TableFormer (表構造認識)                                   │
│   - OCR (スキャン文書検出時のみ自動起動)                        │
│   - 15+ フォーマット対応                                       │
│         ↓ 失敗時                                              │
│ FallbackParser (シンプル抽出、常に動作)                         │
│                                                               │
│ + Vision Model (図表の AI 記述、サイズベースのトリガー)          │
│                                                               │
│ 出力: Markdown (メモリ上)                                      │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 2: 構造化出力                                           │
│                                                               │
│ knowledge/                                                    │
│ ├── sources/           ← 完全な Markdown (Agentic Search 用)  │
│ │   ├── report.md                                             │
│ │   └── proposal.md                                           │
│ ├── assets/            ← 抽出された画像                        │
│ │   └── report-p12-chart.png                                  │
│ └── index.json         ← ファイル目録 (Agent 閲覧用)           │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ Phase 3: 智能切分 (Chunking)                                  │
│                                                               │
│ auto 戦略:                                                    │
│   構造スコアリング → heading or recursive を自動選択            │
│   異常 chunk → AI 介入 (oversized / too-small)                 │
│   Enrichment: パス注入 (必須) + LLM 要約 (オプション)           │
│                                                               │
│ 出力: chunks.jsonl (1 行 = 1 chunk JSON)                      │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
双軌出力:
  ✅ sources/*.md    → Agentic Search (grep/glob)
  ✅ chunks.jsonl    → RAG (vector search)
  ✅ index.json      → Agent のファイル発見
```

---

## 4. Phase 1: ドキュメント解析

### 4.1 解析戦略

Docling に全フォーマットの解析を委譲する。拡張子ベースの手動ルーティングはしない（Docling 内部で既に最適なルーティングが実装済み）。

```
DoclingParser (デフォルト、全フォーマット対応)
  ↓ Docling 失敗時 or 未対応フォーマット
FallbackParser:
  1. プレーンテキストとして読み込みを試行 (UTF-8 → Shift-JIS → Latin-1)
  2. 読めた → そのまま Markdown として出力
  3. 読めない (バイナリ等) → errors.json に記録してスキップ
```

### 4.2 OCR 制御

Docling 内蔵 OCR を利用。設定で OCR エンジンと言語を指定可能。

```yaml
ocr:
  engine: "auto"          # auto | easyocr | tesseract | rapidocr
  languages: ["eng", "jpn", "chi_sim"]
  force: false            # true: テキスト層がある PDF でも OCR 強制
```

- `auto`: EasyOCR → Tesseract → RapidOCR の順に試行
- スキャン文書は Docling が自動検出して OCR 起動（プログラム判断）
- `force: true` は品質確認用（テキスト層が壊れている PDF 向け）

### 4.3 言語検出

```yaml
parsing:
  language_detection: "auto"    # auto | 固定値 ("ja", "en", "zh", etc.)
```

- `auto`: テキストの文字コード分布で簡易検出（AI 不要、高速）
  - CJK 文字比率で日本語/中国語/韓国語を判別
  - ラテン文字ベースは英語をデフォルト
- 固定値指定: 全ドキュメントに同一言語を設定（社内文書が単一言語の場合に便利）
- 検出結果は index.json と chunks.jsonl の `language` フィールドに反映

### 4.4 ページ単位の Vision 戦略

PDF/PPT の各ページは内容が異なるため、**ページごとに最適な処理方法を自動判断**する。全ページ一律に Vision を呼ぶのは非効率（純テキストページは Docling 直接抽出の方が速く・安く・正確）。

```yaml
vision:
  enabled: true
  page_strategy: "auto"        # auto | all_pages | images_only | never
  image_area_threshold: 0.3    # 画像面積占比 > 30% → ページ全体を Vision に送る
  min_image_size_kb: 20        # 20KB 未満の画像 → アイコン/装飾としてスキップ
  min_dimensions: [200, 200]   # 200x200px 未満 → スキップ
  parallel_calls: 8            # Vision API 並列呼出数（ページ分散処理）
```

#### page_strategy モード

| モード | 各ページの判断 | コスト | 適用 |
|--------|--------------|--------|------|
| **`auto`** (推奨) | ページごとに自動判断（下記フロー） | **最小**（必要な分だけ） | デフォルト |
| `all_pages` | 全ページを Vision に送る | **最大** | 高価値文書（契約書、財務報告）|
| `images_only` | 独立画像のみ Vision、ページ全体は送らない | 低 | テキスト中心の文書 |
| `never` | Vision 不使用 | ゼロ | テキストのみで十分な場合 |

#### auto モードのページ判断フロー

```
各ページ
  ↓
テキスト層あり？
  ├─ あり → Docling でテキスト抽出（高速・無料・高精度）
  │   ↓
  │   画像/図表あり？
  │   ├─ なし → 完了（Vision 不要）
  │   ├─ 画像面積 > threshold → ページ全体を Vision に送る
  │   └─ 小さい図表のみ → 図表だけ Vision で記述
  │
  └─ なし（スキャン） → ページ全体を Vision または OCR
```

#### 並列分散処理

Vision が必要なページのみをキューに入れ、`parallel_calls` 数で並列処理:

```
100 ページ PDF:
  70 ページ（純テキスト）→ Docling 一括抽出 → ~3s, 無料
  30 ページ（図表あり） → Vision キューに投入
    → 8 並列で処理 → 30 / 8 × 3s ≈ 12s, ~$0.60

  合計: ~15s, $0.60

  vs 全ページ Vision: ~38s, $2.00 → 60% 高速、70% 低コスト
```

#### Vision 記述の出力例

```markdown
![図表](assets/report-p12-chart.png)

> [図表記述] 棒グラフ: 2023-2025 年の売上推移。2023 年 120 億、2024 年 145 億（+20.8%）、
> 2025 年 168 億（+15.9%）。クラウド占比は 35% → 52% に拡大。
```

### 4.5 各ドキュメント固有の処理

| 形式 | Docling の処理 | Vision | 追加設定 |
|------|---------------|--------|---------|
| PDF (テキスト) | 直接テキスト抽出 + レイアウト分析 | 不要 | `pdf.table_extraction` |
| PDF (表格/図表) | テキスト部分は抽出、図表は Vision | auto で自動判断 | `vision.page_strategy` |
| PDF (スキャン) | 自動 OCR 起動 | auto: 全ページ Vision or OCR | `ocr.engine`, `ocr.languages` |
| PPTX (テキスト中心) | スライド単位でテキスト抽出 | 不要 | `pptx.include_notes` |
| PPTX (図表重い) | テキスト抽出 + 図表ページは Vision | auto で自動判断 | `vision.image_area_threshold` |
| XLSX/CSV | 行テキスト変換 | 不要 | `xlsx.row_to_text`, `xlsx.max_rows` |
| HTML | メインコンテンツ抽出 | 不要 | — |
| 画像 | OCR モード | ページ全体 Vision | `ocr.languages` |
| Markdown/TXT | 透過（そのまま） | 不要 | — |

---

## 5. Phase 2: 構造化出力

### 5.1 出力ディレクトリ

```
knowledge/
├── sources/                         ← Agentic Search 用 (grep/glob 対象)
│   ├── annual-report-2025.md
│   ├── q3-proposal.md
│   └── financial-data.md
├── assets/                          ← 抽出画像
│   ├── annual-report-2025-p12-chart.png
│   └── ...
├── index.json                       ← ファイル目録
└── chunks.jsonl                     ← RAG 用 chunk データ
```

### 5.2 Markdown 出力フォーマット

各ファイルに YAML frontmatter を付与:

```markdown
---
source: annual-report-2025.pdf
format: pdf
title: トヨタ自動車 統合報告書 2025
language: ja
pages: 120
processed_at: 2026-04-02T10:30:00Z
---

# 経営戦略

...本文...
```

### 5.3 index.json スキーマ

```json
{
  "version": 1,
  "processed_at": "2026-04-02T10:30:00Z",
  "config_hash": "sha256:abc123...",
  "files": [
    {
      "path": "sources/annual-report-2025.md",
      "original_file": "annual-report-2025.pdf",
      "format": "pdf",
      "title": "トヨタ自動車 統合報告書 2025",
      "language": "ja",
      "pages": 120,
      "tokens_estimated": 45000,
      "sections": ["経営戦略", "財務データ", "ESG"],
      "has_tables": true,
      "has_images": true,
      "chunks_count": 87
    }
  ],
  "stats": {
    "total_files": 15,
    "total_chunks": 342,
    "total_tokens": 175000,
    "errors": 0
  }
}
```

### 5.4 一貫性保証

Phase 2 と Phase 3 は**同一のメモリ上 Markdown** から出力を生成する。ファイルから再読み込みしない。

```
Docling 解析 → Markdown (メモリ)
  ├─ 書き込み → sources/*.md (Phase 2)
  └─ 切分 → chunks.jsonl (Phase 3)
```

---

## 6. Phase 3: 智能切分 (Chunking)

### 6.1 全体フロー

切分は**2 段階**で決定する。まず「どの切分戦略を使うか」を判断し、次に「切分結果を検証・修正」する。

```
Markdown 入力 (+ 元ファイル形式の情報)
  │
  ▼
Stage 1: 戦略選択
  │
  ├─ strategy が明示指定 → その戦略を使用
  │
  └─ strategy = "auto" → 自動判断:
      │
      ├─ 元が PPTX → slide 切分
      ├─ 元が XLSX/CSV → sheet/行グループ切分
      ├─ 元が 画像 → 全体 = 1 chunk
      └─ 元が PDF/HTML/MD/TXT → 構造スコアリング:
          ├─ スコア ≥ threshold → heading 切分
          └─ スコア < threshold → recursive 切分
  │
  ▼
Stage 2: 切分実行 + 保護ルール
  │
  ├─ テーブル保護: Markdown 表格は分割しない
  ├─ コードブロック保護: ``` ブロックは分割しない
  └─ 異常検出: oversized / too-small → AI 介入 or フォールバック
  │
  ▼
Stage 3: Enrichment
  │
  ├─ パス注入 (必須)
  └─ LLM 要約 (オプション)
  │
  ▼
出力: chunks.jsonl
```

### 6.2 auto 戦略：元ファイル形式による分岐

auto はまず**元ファイルの形式**を見る（Phase 2 の index.json に記録された `format` フィールド）。形式ごとに最適な切分戦略が異なるため:

```yaml
chunking:
  auto:
    # 形式別デフォルト戦略（上書き可能）
    format_strategies:
      pptx: "slide"
      xlsx: "sheet"
      csv: "sheet"
      image: "whole"
      default: "scoring"      # 上記以外はすべて構造スコアリングで判断

    # "image" に該当する拡張子（設定で追加・変更可能）
    image_formats: ["png", "jpg", "jpeg", "tiff", "bmp", "webp", "gif"]
```

| 元形式 | auto が選ぶ戦略 | 理由 |
|--------|---------------|------|
| PPTX | `slide` | 1 スライド = 1 意味単位（業界ベストプラクティス） |
| XLSX/CSV | `sheet` | 行グループ or シート単位が自然な区切り |
| 画像 | `whole` | 1 ファイル = 1 chunk（通常 Vision 記述済み） |
| PDF/HTML/MD/TXT | 構造スコアリング → `heading` or `recursive` | 構造の有無で判断 |

### 6.3 構造スコアリング（PDF/HTML/MD/TXT 用）

ハードコードではなく**スコアリング + 設定可能な閾値**:

```
スコア計算:
  - 見出し数 ≥ min_headings (default: 3)           → +1
  - 見出し階層がジャンプしない (gap ≤ max_gap)       → +1
  - 見出し間コンテンツが適切 (100-2000 token)        → +1

判定:
  スコア ≥ prefer_heading_threshold (default: 2) → heading 戦略
  スコア < threshold                             → recursive 戦略
```

```yaml
chunking:
  auto:
    min_headings: 3
    max_heading_gap_levels: 2
    prefer_heading_threshold: 2
```

### 6.4 切分戦略一覧

| 戦略 | 動作 | 適用 |
|------|------|------|
| **`auto`** | 元形式 + 構造スコアリングで最適戦略を自動選択 | **デフォルト** |
| `heading` | Markdown 見出しで分割 → 各セクション内で recursive | 構造化文書 (PDF/HTML/MD) |
| `recursive` | 段落 → 文 の境界を尊重して 512t 単位で再帰分割 + overlap | 非構造化テキスト |
| `slide` | スライド区切りで分割、1 slide = 1 chunk | PPTX |
| `sheet` | シート/行グループ単位で分割 | XLSX/CSV |
| `whole` | ファイル全体 = 1 chunk | 画像、短いテキスト |
| `agentic` | LLM が最適な分割点を判断（プロンプト詳細は実装時に定義） | 高価値文書のみ |

#### heading 戦略の詳細

```
## 見出し A (300 token) → chunk 1
### 見出し A-1 (200 token)
### 見出し A-2 (150 token)
  ↑ 合計 650 token < max (OK)

## 見出し B (1500 token) → chunk 2... (セクション内で recursive)
  ↑ セクション > max_tokens → セクション内を recursive で分割

## 見出し C (50 token)
## 見出し D (80 token)
  ↑ 両方 < min_tokens だが、見出し境界は尊重（短くても独立 chunk）
```

ルール:
- 見出しレベル（`heading.levels: [1, 2, 3]`）で分割
- セクション内が max_tokens 超過 → セクション内を recursive で再分割（overlap 適用）
- セクション間は overlap **なし**（見出し境界は明確な意味的区切り）
- セクションが min_tokens 未満 → **そのまま保持**（見出し境界は意味的に重要なので結合しない）

#### recursive 戦略の詳細

分割の境界優先順位（上ほど優先）:

```
1. 段落境界（空行 "\n\n"）
2. 文境界（"。" "." "！" "？" + 改行）
3. 文中（最終手段、max_tokens に達した場合のみ）
```

文の途中で切ることは極力避ける。

#### slide 戦略の詳細

```
slide chunker のスライド境界検出（優先順位順）:
  1. Docling 固有のスライド区切りマーカー（あれば）
  2. 水平線 "---" パターン
  3. "# Slide N" / "## スライド N" の見出しパターン
  4. すべて不在 → recursive にフォールバック

各スライド chunk:
  - > max_tokens → スライド内で recursive
  - < min_tokens → そのまま保持（スライドは独立意味単位）
  - 備注（speaker notes）はスライドの chunk 末尾に付加
```

#### sheet 戦略の詳細

```
Excel/CSV → Markdown 変換時にシート名 + ヘッダー行を保持
  ↓
sheet chunker:
  - シートごとに分割（マルチシートの場合）
  - 各シート内は行グループ単位で分割
  - ヘッダー行は各 chunk の先頭に繰り返し付加（コンテキスト保持）
  - 行数が多い場合: max_tokens 以内の行グループに分割
```

### 6.5 保護ルール（切分禁止ゾーン）

どの戦略でも共通で適用される**不可分割ブロック**:

| ブロック | 判定方法 | 処理 |
|---------|---------|------|
| **Markdown テーブル** | `\|` で始まる連続行 | 表全体を 1 chunk に保持。max_tokens 超過しても分割しない（allowed_overflow） |
| **コードブロック** | ` ``` ` で囲まれた範囲 | ブロック全体を 1 chunk に保持 |
| **リスト** | `- ` / `1. ` の連続 | リスト全体をなるべく保持（max_tokens 超過時のみリスト項目間で分割） |
| **引用ブロック** | `> ` の連続行 | ブロック全体を保持 |

```yaml
chunking:
  protection:
    tables: true          # テーブルを分割しない
    code_blocks: true     # コードブロックを分割しない
    lists: true           # リストをなるべく保持
    quotes: true          # 引用ブロックを保持
    allowed_overflow: 2.0 # 保護ブロックは max_tokens × 2.0 まで超過を許容
```

### 6.6 AI 介入（異常 chunk の修正）

AI は**全 chunk に関与しない**。異常時のみ介入:

```yaml
ai_assist:
  enabled: true
  trigger: "oversized"        # oversized | anomaly | always | never
  oversized_multiplier: 2     # chunk > max_tokens × 2 でトリガー
```

| trigger | 介入条件 | AI コスト |
|---------|---------|----------|
| `oversized` | chunk > max_tokens × 2（保護ルールによる超過は除外） | 最小 |
| `anomaly` | oversized + too-small (<min_tokens) + トピック混在検出 | 中 |
| `always` | 全 chunk を AI 検証 | 高（非推奨） |
| `never` | AI 介入なし | ゼロ |

### 6.7 Chunk サイズガイド

| クエリタイプ | 推奨サイズ | 根拠 |
|-------------|-----------|------|
| 事実型（"X は何？"） | 256-512 token | 小 chunk で精確マッチ |
| 分析型 / 多段階（"X と Y を比較"） | 512-1024 token | 大 chunk で完全なコンテキスト |
| 汎用デフォルト | **512 token** | 2026 Vecta ベンチマーク最強 (69%) |

### 6.8 Small-to-Big（父文書検索）— オプション

下流の RAG システムが Small-to-Big 検索をサポートする場合に有効化:

```yaml
chunking:
  small_to_big:
    enabled: false           # デフォルト OFF
    child_tokens: 100        # 小 chunk（精確検索用）
    parent_tokens: 512       # 大 chunk（LLM コンテキスト用）
```

有効化時の出力:
- chunks.jsonl に `parent_id` フィールドが追加される
- 検索時: child chunk でマッチ → parent chunk を LLM に渡す
- **DocIngest 自体は検索しない**。parent_id を付与するだけで、活用は下流システムに委譲

### 6.9 Chunk Enrichment（増強）

```yaml
enrichment:
  path_injection: true          # 必須（無料、全 chunk に適用）
  contextual_summary: false     # LLM 要約（高コスト、オプション）
```

**パス注入**（必須、ゼロコスト）:

```
元の chunk:
  "営業利益は前年比 15% 増加..."

注入後:
  [来源: sources/annual-report-2025.md > 第4章 財務データ > 営業利益]
  "営業利益は前年比 15% 増加..."
```

### 6.10 chunks.jsonl スキーマ

1 行 = 1 JSON オブジェクト:

```json
{
  "id": "annual-report-2025_chunk_012",
  "text": "[来源: sources/annual-report-2025.md > 第4章 財務データ > 営業利益]\n営業利益は...",
  "metadata": {
    "source": "sources/annual-report-2025.md",
    "original_file": "annual-report-2025.pdf",
    "format": "pdf",
    "title_path": "第4章 財務データ > 営業利益",
    "page": 45,
    "chunk_index": 12,
    "total_chunks": 87,
    "tokens": 487,
    "language": "ja",
    "has_table": false,
    "has_image_ref": false,
    "parent_id": null
  }
}
```

Small-to-Big 有効時は `parent_id` に親 chunk の ID が入る。

---

## 7. AI モデル設定

### 7.1 マルチプロバイダー対応

各 AI 利用箇所に独立したモデル設定 + fallback チェーン:

```yaml
models:
  vision:
    primary:
      provider: "google"
      model: "gemini-3-flash"
      api_key_env: "GEMINI_API_KEY"
    fallback:
      provider: "openai"
      model: "gpt-5.4-mini"
      api_key_env: "OPENAI_API_KEY"

  chunking_assist:
    primary:
      provider: "google"
      model: "gemini-3-flash"
      api_key_env: "GEMINI_API_KEY"
    fallback:
      provider: "openai"
      model: "gpt-5.4-mini"
      api_key_env: "OPENAI_API_KEY"
```

- primary 失敗 → 自動で fallback に切替
- 各機能で異なるプロバイダー/モデルを指定可能
- API キーは環境変数から取得（設定ファイルに秘密情報を書かない）

### 7.2 キャッシュ

```yaml
cache:
  enabled: true
  dir: ".docingest_cache"
```

キャッシュキー = `sha256(model_id + file_content_hash + relevant_config_hash)`

- ファイル内容が変わる → hash が変わる → キャッシュミス → 再処理
- 設定が変わる → config_hash が変わる → キャッシュミス
- 同じファイル + 同じ設定 → キャッシュヒット → AI 呼び出しゼロ

---

## 8. エラーハンドリング

```yaml
error_handling:
  on_parse_failure: "skip"      # skip | retry | fail
  on_vision_failure: "skip"     # 図表記述失敗 → スキップ（テキストは残る）
  on_chunk_failure: "fallback"  # 切分失敗 → recursive にフォールバック
  max_retries: 2
  report_file: "errors.json"
```

原則:
- **1 ファイルの失敗が全体を止めない**
- 解析失敗 → スキップして次へ（errors.json に記録）
- Vision 失敗 → 画像記述なしで続行（テキスト部分は正常出力）
- Chunking 失敗 → recursive 戦略にフォールバック
- 全エラーを errors.json に集約（後から確認・再処理可能）

---

## 9. パフォーマンス

```yaml
performance:
  parallel_files: 4              # ファイル単位の並列処理

# Vision 並列数は vision.parallel_calls で設定（デフォルト: 8）
```

二段階の並列化:

```
Level 1: ファイル並列（parallel_files: 4）
  ├─ file_A.pdf → Pipeline 実行
  ├─ file_B.pptx → Pipeline 実行
  ├─ file_C.pdf → Pipeline 実行
  └─ file_D.xlsx → Pipeline 実行

Level 2: ページ並列（vision.parallel_calls: 8）
  file_A.pdf 内:
    ├─ page_3 (図表あり) → Vision API ─┐
    ├─ page_7 (図表あり) → Vision API  ├─ 8 並列
    ├─ page_12 (図表あり) → Vision API ─┘
    └─ 他 97 ページ → Docling 一括（Vision 不要）
```

- Docling はバッチ処理対応（複数ページを一括解析）
- Vision API は**必要なページだけ**キューに入れ並列呼出
- ファイル間は独立なので安全に並列化可能
- 純テキストファイル（.md, .txt）は並列キューを消費しない

---

## 10. プロジェクト構造

```
DocIngest/
├── README.md
├── DESIGN.md                    # 本ドキュメント
├── config/
│   └── default.yaml             # デフォルト設定
├── src/
│   ├── __init__.py
│   ├── cli.py                   # CLI エントリ
│   ├── config.py                # 設定ロード (default < project < CLI args)
│   ├── pipeline.py              # メイン Pipeline 編成
│   ├── parsers/                 # Phase 1: 解析 (可插拔)
│   │   ├── __init__.py          # Parser レジストリ
│   │   ├── base.py              # BaseParser 抽象クラス
│   │   ├── docling_parser.py    # Docling アダプタ
│   │   ├── text_parser.py       # テキスト/Markdown 透過
│   │   └── vision.py            # Vision Model 呼出
│   ├── chunkers/                # Phase 3: 切分 (可插拔)
│   │   ├── __init__.py          # Chunker レジストリ + auto ルーティング
│   │   ├── base.py              # BaseChunker 抽象クラス + 保護ルール
│   │   ├── recursive.py         # 再帰文字切分 (デフォルト)
│   │   ├── heading.py           # 見出し切分 + 再帰フォールバック
│   │   ├── slide.py             # PPTX スライド切分
│   │   ├── sheet.py             # XLSX/CSV シート・行グループ切分
│   │   ├── agentic.py           # LLM 補助切分
│   │   └── validator.py         # 切分結果検証 + AI 修正
│   ├── enrichment/              # Chunk 増強
│   │   ├── __init__.py
│   │   ├── path_injector.py     # パス注入 (必須)
│   │   └── contextual.py        # LLM 要約 (オプション)
│   ├── models/                  # AI モデル抽象層
│   │   ├── __init__.py
│   │   ├── provider.py          # マルチプロバイダー
│   │   └── cache.py             # 呼出キャッシュ
│   └── output/                  # Phase 2: 出力管理
│       ├── __init__.py
│       ├── markdown_writer.py
│       ├── index_builder.py
│       └── chunks_writer.py
├── knowledge/                   # デフォルト出力
│   ├── sources/
│   └── assets/
└── tests/
```

---

## 11. 技術スタック

| コンポーネント | 選択 | 理由 |
|--------------|------|------|
| 言語 | **Python** | Docling が Python、AI SDK エコシステム最強 |
| 文書解析 | **Docling** | 総合最強、15+ フォーマット、AI 版面分析 |
| AI 呼出 | **litellm** or 直接 API | マルチプロバイダー統一 IF |
| CLI | **click** or **typer** | コマンドライン引数解析 |
| 設定 | **PyYAML** | YAML 設定読込 |
| キャッシュ | **diskcache** or 自作 | AI 呼出キャッシュ |

---

## 12. 使用イメージ

```bash
# 基本
docingest ./docs/ -o ./knowledge/

# 設定指定
docingest ./docs/ -c ./my-config.yaml

# chunking 無効
docingest ./docs/ --no-chunks

# 特定ファイル
docingest ./docs/report.pdf ./docs/proposal.pptx
```

---

## 13. スコープ外

| やらないこと | 理由 |
|-------------|------|
| Embedding / ベクトルインデックス | RAG システム側の責務 |
| 検索 | 前処理のみ |
| Late Chunking | Embedding モデルとの連携が必要 → RAG システム側の責務 |
| 跨粒度多層索引 | 同一文書を複数粒度でインデックス → RAG システム側の責務 |
| Web クロール | ローカルファイルのみ |
| リアルタイム監視 | バッチ処理 |
| GUI | CLI のみ |

---

## 14. 根拠資料

- Chunking: [Vecta 2026.02 ベンチマーク](https://www.runvecta.com/blog/we-benchmarked-7-chunking-strategies-most-advice-was-wrong) — 再帰 512t が 69% で最強
- Chunking: [Vectara NAACL 2025](https://aclanthology.org/2025.findings-naacl.114/) — セマンティック切分はコスト非効率
- 解析: [Docling vs LlamaParse vs Unstructured](https://llms.reducto.ai/document-parser-comparison) — Docling 表格 97.9%
- 検索: [A-RAG (arXiv 2602.03442)](https://arxiv.org/abs/2602.03442) — 階層的検索 +5-13%
- 検索: [Amazon Science](https://www.amazon.science/publications/keyword-search-is-all-you-need-achieving-rag-level-performance-without-vector-databases-using-agentic-tool-use) — keyword 検索で RAG 90%+
- 詳細: [RAG-Document-Preprocessing-2026.md](../RAG-Document-Preprocessing-2026.md)
