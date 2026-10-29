#!/usr/bin/env python3
"""GoPro Quick のタイムコード同期で撮影した複数動画を、タイムコード基準で
時刻合わせ・トリミングし、
  1. 解析用: カメラごとの同期済み動画 (解像度・画質を個別に指定可能)
  2. 確認用: 全カメラを縦(または横)に並べた低解像度の概観動画
を書き出す。

各入力は 1 回だけデコードし、ffmpeg 1 回の実行で全出力を作る(中間ファイルなし)。
入力側 -ss + 再エンコードなのでフレーム精度でカットされる
(-c copy だとキーフレーム単位にずれ、編集リストが残るため使わない)。

同期済み動画は OpenCV でフレーム番号がずれないよう、
  - 全カメラで同じフレーム数 (フレーム i が同じ時刻)
  - 固定フレームレート、タイムスタンプ 0 始まり
  - B フレームなし (表示順 = デコード順)、短い GOP (シークが正確)
  - 映像・音声以外のトラック (tmcd, GPMF) は含めない
で出力し、書き出し後にフレーム数を検証する。

例:
  ./gopro_sync_stack.py a.MP4 b.MP4 c.MP4                      # 全カメラ原寸 + 概観動画
  ./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --res orig,orig,720p --crf 18,18,26
  ./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --res none           # 概観動画のみ
  ./gopro_sync_stack.py a.MP4 b.MP4 c.MP4 --no-stack           # 同期済み動画のみ
  ./gopro_sync_stack.py a.MP4 b.MP4 --dry-run                  # オフセット確認のみ
  ./gopro_sync_stack.py a.MP4 b.MP4 --start 1:30 --duration 60 --label
"""
import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

DAY = 24 * 3600


@dataclass
class Clip:
    path: Path
    timecode: str
    fps: Fraction           # 実フレームレート (例: 30000/1001)
    width: int              # 回転適用後のサイズ
    height: int
    duration: float
    has_audio: bool
    start: Fraction = Fraction(0)   # タイムコード開始時刻 [s]
    offset: Fraction = Fraction(0)  # 先頭から切り捨てる秒数
    # 同期済み動画の設定 (res が None なら書き出さない)
    res: str | None = "orig"
    crf: int = 18
    out: Path | None = None
    frames: int = 0


def run_ffprobe(args):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", *args],
                             check=True, capture_output=True, text=True).stdout
    except FileNotFoundError:
        sys.exit("ffprobe が見つかりません。ffmpeg をインストールしてください (例: sudo apt install ffmpeg)")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ffprobe 失敗: {args[-1]}\n{e.stderr}")
    return json.loads(out)


def probe(path):
    info = run_ffprobe(["-show_format", "-show_streams", str(path)])
    streams = info["streams"]
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    if video is None:
        sys.exit(f"映像ストリームがありません: {path}")

    # GoPro は tmcd トラックと映像ストリームの両方に timecode タグを持つ
    tc = None
    for s in [video] + streams + [info["format"]]:
        tc = s.get("tags", {}).get("timecode")
        if tc:
            break
    if not tc:
        sys.exit(f"タイムコードが見つかりません: {path}\n"
                 "GoPro Quick でタイムコード同期した状態で撮影したか確認してください。")

    w, h = video["width"], video["height"]
    rotation = 0
    for sd in video.get("side_data_list", []):
        rotation = int(sd.get("rotation", rotation))
    rotation = int(video.get("tags", {}).get("rotate", rotation))
    if abs(rotation) % 180 == 90:
        w, h = h, w

    return Clip(
        path=Path(path),
        timecode=tc,
        fps=Fraction(video["r_frame_rate"]),
        width=w,
        height=h,
        duration=float(info["format"]["duration"]),
        has_audio=any(s["codec_type"] == "audio" for s in streams),
    )


def timecode_to_frames(tc, fps):
    """SMPTE タイムコード -> 0 時からの通しフレーム数。';' / '.' はドロップフレーム。"""
    m = re.fullmatch(r"(\d+):(\d+):(\d+)([:;.,])(\d+)", tc)
    if not m:
        raise ValueError(f"不正なタイムコード: {tc}")
    hh, mm, ss, sep, ff = m.groups()
    hh, mm, ss, ff = int(hh), int(mm), int(ss), int(ff)
    nominal = round(fps)  # 29.97 -> 30, 59.94 -> 60
    frames = ((hh * 60 + mm) * 60 + ss) * nominal + ff
    if sep in ";.,":
        # 10 分ごとの分を除き、毎分先頭で 2 (29.97) / 4 (59.94) フレームを飛ばす
        drop = round(nominal / 15)
        minutes = hh * 60 + mm
        frames -= drop * (minutes - minutes // 10)
    return frames


def frames_to_timecode(frames, fps, drop):
    nominal = round(fps)
    if drop:
        d = round(nominal / 15)
        per10 = nominal * 600 - d * 9
        per1 = nominal * 60 - d
        tens, rem = divmod(frames, per10)
        frames += d * 9 * tens + (d * ((rem - d) // per1) if rem > d else 0)
    s, ff = divmod(frames, nominal)
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}{';' if drop else ':'}{ff:02d}"


def parse_time(text):
    """'90', '1:30', '0:01:30.5' -> 秒"""
    sec = 0.0
    for part in text.split(":"):
        sec = sec * 60 + float(part)
    return sec


def compute_sync(clips, start, duration):
    for c in clips:
        c.start = timecode_to_frames(c.timecode, c.fps) / c.fps
    # 日付またぎ (23:59 -> 00:00) 対策
    if max(c.start for c in clips) - min(c.start for c in clips) > DAY / 2:
        for c in clips:
            if c.start < DAY / 2:
                c.start += DAY

    t0 = max(c.start for c in clips) + Fraction(start).limit_denominator(1000)
    for c in clips:
        # 各入力のフレーム境界に丸める
        c.offset = round((t0 - c.start) * c.fps) / c.fps
        if c.offset < 0:
            sys.exit("--start が負すぎて一部の動画の開始前になります")

    available = min(c.duration - float(c.offset) for c in clips)
    if available <= 0:
        sys.exit("動画同士の撮影時間が重なっていません")
    if duration is not None:
        if duration > available:
            print(f"⚠️  --duration {duration:.3f}s は共通区間 {available:.3f}s を超えるため切り詰めます")
        available = min(duration, available)
    # 1 フレーム分の余裕を取り、全カメラで実在するフレームだけを書き出す
    for c in clips:
        c.frames = int(available * c.fps) - 1
    return t0, available


def per_input(text, n, name):
    """'a,b,c' を入力数ぶんのリストにする。1 個だけなら全入力に適用。"""
    values = [v.strip() for v in text.split(",")]
    if len(values) == 1:
        return values * n
    if len(values) != n:
        sys.exit(f"--{name} は 1 個か入力数 ({n} 個) をカンマ区切りで指定してください: {text}")
    return values


def res_filter(res):
    """--res の値 -> scale フィルタ (原寸なら None)"""
    if res == "orig":
        return None
    if m := re.fullmatch(r"(\d+)p", res):
        return f"scale=-2:{m[1]}"
    if m := re.fullmatch(r"(\d+)x(\d+)", res):
        return f"scale={m[1]}:{m[2]}"
    if res.isdigit():
        return f"scale={res}:-2"
    sys.exit(f"不正な --res の値: {res} (orig / none / 1920 / 1920x1080 / 720p)")


def res_size(c):
    if c.res == "orig":
        return c.width, c.height
    if m := re.fullmatch(r"(\d+)p", c.res):
        h = int(m[1])
        return round(c.width * h / c.height / 2) * 2, h
    if m := re.fullmatch(r"(\d+)x(\d+)", c.res):
        return int(m[1]), int(m[2])
    w = int(c.res)
    return w, round(c.height * w / c.width / 2) * 2


def build_filter(clips, args, stack_size, fps_out):
    parts, pads = [], []
    for i, c in enumerate(clips):
        branches = (["s"] if c.out else []) + (["p"] if args.stack else [])
        src = f"[{i}:v]"
        if len(branches) == 2:
            parts.append(f"{src}split=2[s{i}in][p{i}in]")
        if c.out:
            s_in = f"[s{i}in]" if len(branches) == 2 else src
            chain = ",".join(f for f in [res_filter(c.res), f"fps={c.fps}", "setsar=1"] if f)
            parts.append(f"{s_in}{chain}[s{i}]")
        if args.stack:
            p_in = f"[p{i}in]" if len(branches) == 2 else src
            scale = f"scale={stack_size}:-2" if args.layout == "vstack" else f"scale=-2:{stack_size}"
            chain = f"{p_in}{scale},setsar=1,fps={fps_out}"
            if args.label:
                # フィルタ文字列のエスケープを避けるため記号は '_' に置換
                text = re.sub(r"[^\w.\- ]", "_", c.path.stem)
                chain += (f",drawtext=text='{text}':x=10:y=10:fontsize=h/18:"
                          "fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=6")
            parts.append(f"{chain}[p{i}]")
            pads.append(f"[p{i}]")
    if args.stack:
        if len(pads) == 1:
            parts.append(f"{pads[0]}null[stack]")
        else:
            parts.append(f"{''.join(pads)}{args.layout}=inputs={len(pads)}[stack]")
    return ";".join(parts)


def video_codec_args(vcodec, crf, preset):
    if "nvenc" in vcodec or "qsv" in vcodec:
        return ["-c:v", vcodec, "-cq", str(crf)]
    return ["-c:v", vcodec, "-crf", str(crf), "-preset", preset]


def build_command(clips, args, t_len, stack_size, fps_out):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
           "-y" if args.overwrite else "-n"]
    for c in clips:
        if args.hwaccel:
            cmd += ["-hwaccel", args.hwaccel]
        cmd += ["-ss", f"{float(c.offset):.6f}", "-i", str(c.path)]
    cmd += ["-filter_complex", build_filter(clips, args, stack_size, fps_out)]

    for i, c in enumerate(clips):
        if not c.out:
            continue
        cmd += ["-map", f"[s{i}]"]
        if c.has_audio:
            cmd += ["-map", f"{i}:a:0", "-c:a", "aac", "-b:a", "192k"]
        cmd += video_codec_args(args.vcodec, c.crf, args.preset)
        # 解析向け: B フレームなし・短い GOP・フレーム数固定・0 始まり・余計なトラックなし
        cmd += ["-bf", "0", "-g", str(round(c.fps * args.gop)),
                "-frames:v", str(c.frames), "-t", f"{float(c.frames / c.fps):.6f}",
                # -avoid_negative_ts make_zero は AAC のプライミング分 (約 21ms) 映像側を
                # 後ろにずらし空の編集リストを作るので使わない (プライミングは音声トラック側で処理される)
                "-pix_fmt", "yuv420p", "-map_metadata", "-1", "-movflags", "+faststart", str(c.out)]

    if args.stack:
        cmd += ["-map", "[stack]"]
        if args.audio is not None:
            cmd += ["-map", f"{args.audio}:a:0", "-c:a", "aac", "-b:a", "128k"]
        cmd += video_codec_args(args.vcodec, args.stack_crf, args.preset)
        cmd += ["-t", f"{t_len:.6f}", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(args.output)]
    return cmd


def run_ffmpeg(cmd):
    """ffmpeg を実行し、無害な警告だけ除いて stderr (進捗表示を含む) をそのまま流す。

    GoPro の映像は yuvj420p (フルレンジ) で、swscale が通るたびに
    "deprecated pixel format used" が出るが、レンジは正しく引き継がれるので表示しない。
    """
    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    except FileNotFoundError:
        sys.exit("ffmpeg が見つかりません。ffmpeg をインストールしてください")
    out = sys.stderr.buffer
    buf = b""
    while chunk := proc.stderr.read1(4096):
        buf += chunk
        # 進捗表示は \r 区切りなので \r / \n の両方で行に分ける
        *lines, buf = re.split(rb"(?<=[\r\n])", buf)
        for line in lines:
            if b"deprecated pixel format used" not in line:
                out.write(line)
        out.flush()
    out.write(buf)
    if proc.wait() != 0:
        sys.exit(f"ffmpeg がエラー終了しました (code {proc.returncode})")


def verify(clips):
    """書き出した同期済み動画のフレーム数・開始時刻を確認する。"""
    ok = True
    for c in clips:
        if not c.out:
            continue
        info = run_ffprobe(["-select_streams", "v:0", "-count_packets",
                            "-show_entries", "stream=nb_read_packets,start_time", str(c.out)])
        s = info["streams"][0]
        n, start = int(s["nb_read_packets"]), float(s.get("start_time", 0))
        msg = f"{c.out.name}: {n} frames, start_time={start:.3f}"
        try:
            import cv2
            cap = cv2.VideoCapture(str(c.out))
            n_cv = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()
            msg += f", OpenCV frame count={n_cv}"
        except ImportError:
            n_cv = n
        if n == c.frames and n_cv == c.frames and abs(start) < 1e-3:
            print(f"  ✅ {msg}")
        else:
            print(f"  ❌ {msg} (期待値 {c.frames} frames, start_time=0)")
            ok = False
    return ok


def parse_args():
    p = argparse.ArgumentParser(
        description="GoPro のタイムコードで複数動画を同期・トリミングし、"
                    "カメラごとの解析用動画と、縦に並べた概観動画を書き出す",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:" + __doc__.split("例:", 1)[1],
    )
    p.add_argument("files", nargs="+", metavar="VIDEO", help="入力動画 (概観動画では上から順に並ぶ)")
    p.add_argument("--start", type=parse_time, default=0.0, metavar="T",
                   help="同期点 (全カメラが揃った時刻) からさらに切り捨てる時間。秒 or [HH:]MM:SS")
    p.add_argument("--duration", type=parse_time, metavar="T",
                   help="出力の長さ。省略時は全カメラが揃っている区間の最後まで")

    g = p.add_argument_group("同期済み動画 (解析用、カメラごと)")
    g.add_argument("--res", default="orig", metavar="R[,R...]",
                   help="解像度。orig / none (書き出さない) / 幅 (1920) / 幅x高さ / 高さp (720p)。"
                        "カンマ区切りで入力順に個別指定、1 個なら全入力に適用 (default: orig)")
    g.add_argument("--crf", default="18", metavar="N[,N...]",
                   help="画質 (小さいほど高画質)。--res と同様に個別指定可 (default: 18)")
    g.add_argument("--outdir", type=Path, metavar="DIR",
                   help="出力先ディレクトリ (default: 入力と同じ場所)。ファイル名は <入力名>_synced.mp4")
    g.add_argument("--gop", type=float, default=1.0, metavar="SEC",
                   help="キーフレーム間隔 [s]。短いほど OpenCV のシークが速く正確 (default: 1.0)")

    g = p.add_argument_group("概観動画 (全カメラを並べたもの)")
    g.add_argument("-o", "--output", type=Path, default=Path("stacked.mp4"),
                   help="出力ファイル (default: stacked.mp4)")
    g.add_argument("--no-stack", dest="stack", action="store_false", help="概観動画を作らない")
    g.add_argument("--layout", choices=["vstack", "hstack"], default="vstack",
                   help="vstack=縦並び (default), hstack=横並び")
    g.add_argument("--stack-size", type=int, default=640, metavar="PX",
                   help="各パネルの幅 (vstack) / 高さ (hstack) (default: 640)")
    g.add_argument("--stack-crf", type=int, default=28, help="画質 (default: 28)")
    g.add_argument("--fps", type=Fraction, metavar="FPS",
                   help="フレームレート (例: 30000/1001)。省略時は 1 本目に合わせる")
    g.add_argument("--audio", default="0", metavar="N",
                   help="音声を使う入力の番号 (0 始まり)。'none' で音声なし (default: 0)")
    g.add_argument("--label", action="store_true", help="各パネル左上にファイル名を焼き込む")

    g = p.add_argument_group("エンコード共通")
    g.add_argument("--vcodec", default="libx264",
                   help="映像エンコーダ (libx264, libx265, h264_nvenc など。default: libx264)")
    g.add_argument("--preset", default="medium", help="x264/x265 のプリセット (default: medium)")
    g.add_argument("--hwaccel", metavar="API",
                   help="デコードのハードウェアアクセラレーション (例: cuda, auto)")
    g.add_argument("--overwrite", action="store_true", help="出力ファイルが存在する場合に上書き")
    g.add_argument("--dry-run", action="store_true",
                   help="オフセット表と ffmpeg コマンドを表示するだけで実行しない")

    args = p.parse_args()
    args.audio = None if args.audio.lower() == "none" else int(args.audio)
    if args.audio is not None and not 0 <= args.audio < len(args.files):
        p.error(f"--audio は 0〜{len(args.files) - 1} で指定してください")
    return args


def main():
    args = parse_args()
    clips = [probe(f) for f in args.files]
    n = len(clips)
    for c, res, crf in zip(clips, per_input(args.res, n, "res"), per_input(args.crf, n, "crf")):
        if res.lower() in ("none", "skip"):
            c.res = None
            continue
        res_filter(res)  # 値の検証
        c.res, c.crf = res, int(crf)
        c.out = (args.outdir or c.path.parent) / f"{c.path.stem}_synced.mp4"
    if not args.stack and not any(c.out for c in clips):
        sys.exit("出力がありません (--res none と --no-stack が同時に指定されています)")
    if len({c.out for c in clips if c.out}) < sum(1 for c in clips if c.out):
        sys.exit("同期済み動画のファイル名が重複します。入力ファイル名を変えるか --outdir を確認してください")

    t0, t_len = compute_sync(clips, args.start, args.duration)

    if args.audio is not None and not clips[args.audio].has_audio:
        print(f"⚠️  入力 {args.audio} に音声がないため概観動画は音声なしで出力します")
        args.audio = None

    fps_out = args.fps or clips[0].fps
    if len({c.fps for c in clips}) > 1:
        print("⚠️  フレームレートが混在しています。同期済み動画は各カメラの元のレートのまま、"
              f"概観動画は {float(fps_out):.3f} fps で出力します")

    drop = ";" in clips[0].timecode or "." in clips[0].timecode
    t0_tc = frames_to_timecode(round(t0 * clips[0].fps), clips[0].fps, drop)

    name_w = max(len(c.path.name) for c in clips)
    print(f"{'#':>2}  {'file':<{name_w}}  {'timecode':<11}  {'input':>9}  {'fps':>6}  "
          f"{'cut[s]':>8}  {'cut[f]':>6}  synced output")
    for i, c in enumerate(clips):
        synced = f"{'x'.join(map(str, res_size(c)))} crf={c.crf}" if c.out else "-"
        print(f"{i:>2}  {c.path.name:<{name_w}}  {c.timecode:<11}  {c.width:>4}x{c.height:<4}  "
              f"{float(c.fps):>6.2f}  {float(c.offset):>8.3f}  {round(c.offset * c.fps):>6}  {synced}")
    print(f"\n出力開始タイムコード: {t0_tc}  長さ: {t_len:.3f}s  "
          f"フレーム数: {', '.join(str(c.frames) for c in clips)}")

    stack_size = args.stack_size
    if args.stack:
        if args.layout == "vstack":
            total = sum(round(c.height * stack_size / c.width / 2) * 2 for c in clips)
            out_w, out_h = stack_size, total
        else:
            total = sum(round(c.width * stack_size / c.height / 2) * 2 for c in clips)
            out_w, out_h = total, stack_size
        print(f"概観動画: {args.output}  {out_w}x{out_h}")
        if args.vcodec in ("libx264", "h264_nvenc") and max(out_w, out_h) > 4096:
            print("⚠️  概観動画が 4096px を超えるため、再生できないプレイヤーがあります。--stack-size で縮小を推奨")

    cmd = build_command(clips, args, t_len, stack_size, fps_out)

    if args.dry_run:
        print("\n" + shlex.join(cmd))
        return

    outputs = [c.out for c in clips if c.out] + ([args.output] if args.stack else [])
    existing = [str(o) for o in outputs if o.exists()]
    if existing and not args.overwrite:
        sys.exit("既に存在します (--overwrite で上書き):\n  " + "\n  ".join(existing))
    for o in outputs:
        o.parent.mkdir(parents=True, exist_ok=True)

    print("\n🎞️  エンコード中...", flush=True)
    run_ffmpeg(cmd)

    ok = True
    if any(c.out for c in clips):
        print("\n🔍 同期済み動画の検証")
        ok = verify(clips)

    report = {
        "start_timecode": t0_tc,
        "duration_sec": round(t_len, 6),
        "stack": str(args.output) if args.stack else None,
        "inputs": [{
            "file": str(c.path), "timecode": c.timecode, "fps": str(c.fps),
            "cut_sec": round(float(c.offset), 6), "cut_frames": round(c.offset * c.fps),
            "synced": str(c.out) if c.out else None,
            "synced_size": res_size(c) if c.out else None,
            "synced_frames": c.frames if c.out else None,
        } for c in clips],
    }
    report_path = (args.output if args.stack else next(c.out for c in clips if c.out)).with_suffix(".sync.json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n{'✅ 完了' if ok else '⚠️  検証で不一致あり'}\n📝 同期情報: {report_path}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
