"""Один готовый Reel → 12 вариантов по профилю 010 и проверенный план публикаций."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import uuid

from posting_plan import build_plan, load_captions, verify_plan
from verify_variants import capture, file_hash, primary_streams, probe, require_tools, validate_prefix, verify, write_report


LUMA_FILTER = "lut=y='if(eq(mod(val,28),0),val,val+1)'"
DEFAULT_PROFILE = Path(__file__).resolve().parent / "profiles" / "original_12.json"


def load_profile(path):
    profile = json.loads(path.read_text(encoding="utf-8-sig"))
    variants = profile.get("variants", [])
    if len(variants) != 12 or [v.get("index") for v in variants] != list(range(1, 13)):
        raise ValueError("Профиль должен содержать ровно 12 вариантов с index 1..12")
    for variant in variants:
        for key, low, high in (("crf", 0, 51), ("audio_gain_db", -60, 0)):
            value = variant.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Недопустимый {key} в варианте {variant['index']}")
        if not isinstance(variant.get("luma_shift"), bool):
            raise ValueError("luma_shift должен быть true или false")
    return profile


def encode_command(source, destination, variant):
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n", "-fflags", "+bitexact",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
    ]
    if variant["luma_shift"]:
        command += ["-vf", LUMA_FILTER]
    return command + [
        "-c:v", "libx264", "-preset", "medium", "-crf", str(variant["crf"]),
        "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
        "-x264-params", "keyint=240:min-keyint=24:scenecut=40", "-flags:v", "+bitexact",
        "-af", f"volume={variant['audio_gain_db']:.2f}dB", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-flags:a", "+bitexact", "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata", "title=", "-metadata", "comment=", "-metadata", "description=",
        "-metadata:s:v:0", "handler_name=", "-metadata:s:a:0", "handler_name=",
        "-movflags", "+faststart", str(destination),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="готовый локальный MP4 с видео и аудио")
    parser.add_argument("output", type=Path, help="новая папка результата")
    parser.add_argument("--prefix", default="reel")
    parser.add_argument("--captions", type=Path, required=True, help="JSON с 12 подписями для этого ролика")
    parser.add_argument("--config", type=Path, default=DEFAULT_PROFILE)
    args = parser.parse_args()
    staging = None
    try:
        require_tools()
        validate_prefix(args.prefix)
        source, output = args.source.resolve(), args.output.resolve()
        if not source.is_file():
            raise ValueError(f"Нет исходного файла: {source}")
        if output.exists():
            raise ValueError(f"Папка результата уже существует; укажи новую: {output}")
        profile = load_profile(args.config)
        captions = load_captions(args.captions)
        source_video, source_audio = primary_streams(probe(source))
        if source_video["width"] % 2 or source_video["height"] % 2:
            raise ValueError("Для yuv420p нужны чётные ширина и высота")
        if source_video.get("pix_fmt") != "yuv420p" or source_video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
            raise ValueError("Этот профиль рассчитан на SDR yuv420p 8-bit; сначала подготовь мастер")
        if source_audio["channels"] not in (1, 2):
            raise ValueError("Нужен мастер с mono или stereo аудио")
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = output.with_name(f".{output.name}.partial-{uuid.uuid4().hex[:8]}")
        staging.mkdir()
        manifest = {
            "created_utc": datetime.now(timezone.utc).isoformat(), "source": source.name,
            "source_sha256": file_hash(source), "prefix": args.prefix, "profile": profile,
            "luma_filter": LUMA_FILTER, "captions_sha256": file_hash(args.captions),
            "python": sys.version.split()[0],
            "ffmpeg": capture(["ffmpeg", "-version"]).decode().splitlines()[0],
            "ffprobe": capture(["ffprobe", "-version"]).decode().splitlines()[0],
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for variant in profile["variants"]:
            destination = staging / f"{args.prefix}_u{variant['index']:02d}.mp4"
            print(f"RENDER {variant['index']:02d}/12 {destination.name}", flush=True)
            capture(encode_command(source, destination, variant))
        report = verify(source, staging, args.prefix)
        write_report(staging, report)
        if report["summary"]["status"] != "PASS":
            raise ValueError("; ".join(report["summary"]["errors"]))
        build_plan(staging, args.prefix, captions)
        for _ in range(2):
            errors = verify_plan(staging, captions)
            if errors:
                raise ValueError("; ".join(errors))
        if file_hash(source) != manifest["source_sha256"]:
            raise ValueError("Источник изменился во время обработки")
        # Только полностью проверенная пачка получает имя итоговой папки.
        staging.rename(output)
        print(f"PASS: {output}\n12 MP4 + verification.html + upload_plan.html", flush=True)
    except (ValueError, OSError, KeyError) as exc:
        suffix = f"\nДиагностическая папка: {staging}" if staging and staging.exists() else ""
        parser.exit(1, f"FAIL: {exc}{suffix}\n")


if __name__ == "__main__":
    main()
