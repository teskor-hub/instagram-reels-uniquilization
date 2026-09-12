"""Verify actual MP4 files: decoding, streams, hashes, and visual measurements."""

import argparse
from fractions import Fraction
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import subprocess


HASH_FIELDS = ("container_sha256", "video_sha256", "audio_sha256", "decoded_audio_sha256")


def capture(command):
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def require_tools():
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            raise ValueError(f"{name} was not found in PATH")


def validate_prefix(prefix):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", prefix):
        raise ValueError("prefix: 1–80 Latin letters, digits, '_' or '-'; the first character must be a letter or digit")


def probe(path):
    return json.loads(capture([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stream_hash(path, selector, decoded=False):
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:{selector}:0"]
    if decoded:
        command += ["-c:a", "pcm_s16le", "-ar", "48000"]
    else:
        command += ["-c", "copy"]
    return capture(command + ["-f", "hash", "-hash", "sha256", "-"]).decode().strip().split("=", 1)[1].lower()


def sampled_gray(path):
    return capture([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
        "-vf", "fps=4,scale=64:64,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ])


def visual_metrics(left, right):
    if not left or len(left) != len(right):
        raise ValueError("The visual sample is empty or sample lengths differ")
    length = len(left)
    mae = sum(abs(a - b) for a, b in zip(left, right)) / length / 255
    mean_left, mean_right = sum(left) / length, sum(right) / length
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    variance_left = sum((a - mean_left) ** 2 for a in left)
    variance_right = sum((b - mean_right) ** 2 for b in right)
    denominator = math.sqrt(variance_left * variance_right)
    correlation = round(covariance / denominator, 8) if denominator else None
    return round(mae, 8), correlation


def primary_streams(metadata):
    video = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in metadata["streams"] if s["codec_type"] == "audio"), None)
    if video is None or audio is None:
        raise ValueError("The source must contain video and audio; an empty track is not added automatically")
    return video, audio


def verify(source, output, prefix):
    require_tools()
    validate_prefix(prefix)
    source, output = Path(source), Path(output)
    expected = [f"{prefix}_u{index:02d}.mp4" for index in range(1, 13)]
    actual = sorted(p.name for p in output.glob("*.mp4"))
    if actual != expected:
        raise ValueError("Expected exactly 12 MP4 files named prefix_u01.mp4 … prefix_u12.mp4")
    metadata = probe(source)
    source_video, source_audio = primary_streams(metadata)
    source_duration = float(metadata["format"]["duration"])
    source_frames = sampled_gray(source)
    errors, items, samples = [], [], []
    for index, name in enumerate(expected, 1):
        print(f"CHECK {index:02d}/12 {name}", flush=True)
        path = output / name
        # Полное декодирование в null обнаруживает повреждения за пределами превью.
        capture(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0",
                 "-map", "0:a:0", "-f", "null", "-"])
        info = probe(path)
        video, audio = primary_streams(info)
        duration = float(info["format"]["duration"])
        checks = {
            "two_streams": len(info["streams"]) == 2,
            "h264_high_yuv420p": video["codec_name"] == "h264" and video.get("profile") == "High" and video.get("pix_fmt") == "yuv420p",
            "resolution": (video["width"], video["height"]) == (source_video["width"], source_video["height"]),
            "fps": Fraction(video["avg_frame_rate"]) == Fraction(source_video["avg_frame_rate"]),
            "frame_count": int(video["nb_read_frames"]) == int(source_video["nb_read_frames"]),
            "duration": abs(duration - source_duration) < 0.05,
            "audio": audio["codec_name"] == "aac" and audio["sample_rate"] == "48000" and audio["channels"] == source_audio["channels"],
        }
        errors.extend(f"{name}: {key}" for key, ok in checks.items() if not ok)
        frames = sampled_gray(path)
        samples.append(frames)
        mae, correlation = visual_metrics(source_frames, frames)
        items.append({
            "file": name, "bytes": path.stat().st_size, "duration": duration,
            "resolution": f"{video['width']}x{video['height']}", "fps": video["avg_frame_rate"],
            "frames": int(video["nb_read_frames"]), "checks": checks,
            "container_sha256": file_hash(path), "video_sha256": stream_hash(path, "v"),
            "audio_sha256": stream_hash(path, "a"), "decoded_audio_sha256": stream_hash(path, "a", True),
            "visual_mae": mae, "correlation": correlation,
        })
    pairs = []
    for left in range(12):
        for right in range(left + 1, 12):
            mae, correlation = visual_metrics(samples[left], samples[right])
            pairs.append({"left": expected[left], "right": expected[right], "visual_mae": mae, "correlation": correlation})
    unique = {field: len({item[field] for item in items}) for field in HASH_FIELDS}
    errors.extend(f"{field}: only {count}/12 distinct values" for field, count in unique.items() if count != 12)
    correlations = [p["correlation"] for p in pairs if p["correlation"] is not None]
    summary = {
        "status": "FAIL" if errors else "PASS", "files": 12,
        "unique_containers": unique[HASH_FIELDS[0]], "unique_video_streams": unique[HASH_FIELDS[1]],
        "unique_audio_streams": unique[HASH_FIELDS[2]], "unique_decoded_audio_streams": unique[HASH_FIELDS[3]],
        "source": source.name, "source_sha256": file_hash(source), "source_duration": source_duration,
        "all_duration_matches_source": all(item["checks"]["duration"] for item in items),
        "pairwise_max_visual_mae": max(p["visual_mae"] for p in pairs),
        "pairwise_min_correlation": min(correlations) if correlations else None,
        "errors": errors,
    }
    return {"summary": summary, "files": items, "pairs": pairs}


def write_report(output, report):
    output = Path(output)
    (output / "unique_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    rows = "".join(
        f"<tr><td>{html.escape(item['file'])}</td><td>{item['bytes']:,}</td>"
        f"<td>{item['duration']:.3f}</td><td>{item['frames']}</td>"
        f"<td>{item['visual_mae'] * 100:.6f}%</td><td>{item['correlation']}</td></tr>"
        for item in report["files"]
    )
    page = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>12-Variant Verification</title><style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--text:#172139;--line:#dbe1ee;--accent:#6354d8}
@media(prefers-color-scheme:dark){:root{--bg:#101520;--card:#1a2232;--text:#e9eef8;--line:#354158;--accent:#b3a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui}
main{max-width:1120px;margin:auto;padding:36px 20px}h1{font-size:36px;margin-bottom:8px}a{color:var(--accent)}
.card{padding:20px;background:var(--card);border:1px solid var(--line);border-radius:16px;margin:18px 0;overflow:auto}
table{width:100%;border-collapse:collapse}td,th{padding:10px;text-align:left;border-bottom:1px solid var(--line)}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style><main><p>INSTAGRAM REELS / LOCAL VERIFICATION</p><h1>12 variants · __STATUS__</h1>
<p>Actual files verified: full decoding, video frames, duration, and SHA-256 hashes of containers and streams.</p>
<div class="card"><b>Verification scope.</b> PASS means the technical checks passed.
MAE and correlation are calculated from 4 frames/s in 64×64 grayscale; inspect fine details and colours manually.
This result does not measure Instagram duplicate detection.</div>
<div class="card"><table><thead><tr><th>File</th><th>Bytes</th><th>Seconds</th><th>Frames</th><th>MAE vs. source</th><th>Correlation</th></tr></thead><tbody>__ROWS__</tbody></table></div>
<div class="card"><h2>Summary</h2><pre>__SUMMARY__</pre></div><p><a href="upload_plan.html">Posting plan and captions</a> · <a href="unique_verification.json">All hashes and 66 pairwise comparisons</a></p></main></html>"""
    page = page.replace("__STATUS__", summary["status"]).replace("__ROWS__", rows).replace("__SUMMARY__", html.escape(json.dumps(summary, ensure_ascii=False, indent=2)))
    (output / "verification.html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prefix", default="reel")
    args = parser.parse_args()
    try:
        report = verify(args.source, args.output, args.prefix)
        write_report(args.output, report)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"FAIL: {exc}\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["summary"]["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
