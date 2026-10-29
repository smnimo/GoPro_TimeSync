# GoPro TimeSync

GoPro Quick のタイムコード同期を使って撮影した複数の GoPro 動画を、タイムコード基準で
フレーム単位に時刻合わせ・トリミングするスクリプトです。

- **解析用**: カメラごとの同期済み動画 (解像度・画質をカメラ別に指定可能)
- **確認用**: 全カメラを縦 (または横) に並べた低解像度の概観動画

を ffmpeg 1 回の実行でまとめて書き出します。

## 必要環境

- Python 3.10 以上 (標準ライブラリのみ)
- ffmpeg / ffprobe

```bash
sudo apt install ffmpeg

# 任意 (OpenCV による検証を使う場合)
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate   # 以降 ./gopro_sync_stack.py は .venv の Python で実行される
```

OpenCV (`cv2`) がインストールされている場合は、出力後の検証で OpenCV から見たフレーム数も確認します。
なくても動作します。

## 撮影時の準備

1. GoPro Quick アプリの「タイムコード同期」(QR コード) で全カメラの時刻を合わせる
2. 各カメラで撮影する (開始タイミングはバラバラで良い)

動画には撮影開始時刻のタイムコード (例: `16:01:26;17`) が記録され、これを基準に同期します。

## 使い方

```bash
# 全カメラを原寸で同期 + 概観動画 stacked.mp4
./gopro_sync_stack.py a.MP4 b.MP4 c.MP4

# a, b は原寸・高画質、c は 720p に圧縮。同期済み動画は synced/ へ
./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --res orig,orig,720p --crf 18,18,26 --outdir synced

# オフセットと実行コマンドの確認のみ (エンコードしない)
./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --dry-run

# 同期点から 1分30秒後の 60 秒間だけ、ファイル名ラベル付きで
./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --start 1:30 --duration 60 --label

# 概観動画のみ / 同期済み動画のみ
./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --res none
./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --no-stack
```

実行すると最初にオフセット表が表示されます。

```
 #  file                        timecode         input     fps    cut[s]  cut[f]  synced output
 0  20261026_test_s1_main.MP4   16:01:21;22  3840x2160   29.97     4.838     145  3840x2160 crf=18
 1  20261026_test_s2_main.MP4   16:01:23;26  3840x2160   29.97     2.703      81  3840x2160 crf=18
 2  20261026_test_all_main.MP4  16:01:26;17  3840x2160   29.97     0.000       0  1280x720 crf=26

出力開始タイムコード: 16:01:26;17  長さ: 1420.586s  フレーム数: 42574, 42574, 42574
```

## 出力

| ファイル | 内容 |
|---|---|
| `<入力名>_synced.mp4` | カメラごとの同期済み動画 (解析用)。全カメラで同じフレーム数 |
| `stacked.mp4` (`-o` で変更) | 全カメラを並べた概観動画 |
| `stacked.sync.json` | 各カメラのタイムコード、切り捨てた秒数・フレーム数、出力フレーム数など |

`--no-stack` のときは `.sync.json` は最初の同期済み動画の隣に書き出されます。

同期済み動画のフレーム `i` は、元動画のフレーム `i + cut_frames` に対応します (`cut_frames` は `.sync.json` に記録)。

## オプション

### 共通

| オプション | 説明 |
|---|---|
| `--start T` | 同期点 (全カメラが揃った時刻) からさらに切り捨てる時間。秒 or `[HH:]MM:SS` |
| `--duration T` | 出力の長さ。省略時は全カメラが揃っている区間の最後まで |

### 同期済み動画 (解析用)

| オプション | 説明 |
|---|---|
| `--res R[,R...]` | 解像度。`orig` / `none` (書き出さない) / `1920` (幅) / `1920x1080` / `720p`。カンマ区切りで入力順に個別指定、1 個なら全入力に適用 (default: `orig`) |
| `--crf N[,N...]` | 画質。小さいほど高画質 (default: 18)。`--res` と同様に個別指定可 |
| `--outdir DIR` | 出力先 (default: 入力と同じ場所) |
| `--gop SEC` | キーフレーム間隔 (default: 1.0)。短いほどシークが速いがファイルは大きくなる |

### 概観動画

| オプション | 説明 |
|---|---|
| `-o FILE` | 出力ファイル (default: `stacked.mp4`) |
| `--no-stack` | 概観動画を作らない |
| `--layout vstack\|hstack` | 縦並び (default) / 横並び |
| `--stack-size PX` | 各パネルの幅 (vstack) / 高さ (hstack) (default: 640) |
| `--stack-crf N` | 画質 (default: 28) |
| `--fps FPS` | フレームレート (default: 1 本目に合わせる) |
| `--audio N\|none` | 音声を使う入力の番号 (default: 0) |
| `--label` | 各パネルにファイル名を表示 |

### エンコード

| オプション | 説明 |
|---|---|
| `--vcodec` | `libx264` (default) / `libx265` / `h264_nvenc` など |
| `--preset` | x264/x265 のプリセット (default: `medium`) |
| `--hwaccel API` | デコードのハードウェアアクセラレーション (`cuda`, `auto` など) |
| `--overwrite` | 既存の出力を上書き |
| `--dry-run` | オフセット表と ffmpeg コマンドの表示のみ |

4K をフル尺で再エンコードすると CPU では時間がかかります。NVIDIA GPU があれば
`--hwaccel cuda --vcodec h264_nvenc` で大幅に速くなります。
本番前に `--duration 30` などで短く試すのがおすすめです。

## 解析 (OpenCV) 向けの配慮

`-ss` と `-c copy` でトリミングすると、キーフレームより前から始まる分のフレームと
「表示時はスキップする」という編集リストが残ります。プレイヤーはこれに従いますが、
OpenCV は従わない場合があり、フレーム番号がずれる原因になります。
同期済み動画は次のように出力し、このずれが起きないようにしています。

- 切り出し位置から再エンコード (編集リストなし、タイムスタンプ 0 始まり)
- 全カメラで同じフレーム数 (同じフレーム番号 = 同じ時刻)
- 固定フレームレート
- B フレームなし (表示順 = デコード順)、短い GOP (`CAP_PROP_POS_FRAMES` のシークが正確)
- 映像・音声以外のトラック (タイムコード、GPMF センサーデータ) は含めない

書き出し後、ffprobe (と OpenCV があれば OpenCV) でフレーム数と開始時刻を自動検証し、
不一致があればエラー終了します。

```python
import cv2
caps = [cv2.VideoCapture(p) for p in ["synced/a_synced.mp4", "synced/b_synced.mp4"]]
while True:
    frames = [cap.read() for cap in caps]
    if not all(ok for ok, _ in frames):
        break
    # frames[k][1] はすべて同じ時刻の画像
```

## 注意

- 同期精度は 1 フレームです (タイムコードはフレーム単位のため)。
- 異なるフレームレートのカメラが混在する場合、同期済み動画は各カメラの元のレートのまま出力されます。この場合は同じフレーム番号が同じ時刻になりません。
- 同期済み動画は再エンコードのため元動画からわずかに劣化します。必要に応じて `--crf` を下げてください (14〜18 程度)。
- タイムコードのない動画 (Quick で同期していない動画) はエラーになります。

## ファイル

| ファイル | 説明 |
|---|---|
| `gopro_sync_stack.py` | 本スクリプト |
| `requirements.txt` | 任意の Python 依存 (OpenCV) |
