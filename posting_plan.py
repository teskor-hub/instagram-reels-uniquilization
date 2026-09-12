"""Собирает и проверяет локальный план публикации для 12 готовых Reel.

Подписи пишет редактор. Скрипт не генерирует и не подставляет их автоматически.
"""

import argparse
import html
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path


CAPTION_COUNT = 12
SLOT_OFFSETS = (0, 18_000, 36_000)
MOSCOW_OFFSET = timezone(timedelta(hours=3), name="MSK")
MAX_SEQUENCE_SIMILARITY = 0.86
MAX_TOKEN_JACCARD = 0.65


def _normalise_caption(value: str) -> str:
    """Нормализация для поиска дублей, а не для изменения текста подписи."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


def _caption_body(value: str) -> str:
    without_tags = re.sub(r"#\w+", " ", value, flags=re.UNICODE)
    without_punctuation = re.sub(r"[^\w]+", " ", without_tags, flags=re.UNICODE)
    return _normalise_caption(without_punctuation)


def _caption_tags(value: str) -> tuple[str, ...]:
    return tuple(sorted(set(tag.casefold() for tag in re.findall(r"#\w+", value, flags=re.UNICODE))))


def _caption_similarity(left: str, right: str) -> tuple[float, float]:
    left_body = _caption_body(left)
    right_body = _caption_body(right)
    sequence = SequenceMatcher(None, left_body, right_body).ratio()
    left_tokens = set(left_body.split())
    right_tokens = set(right_body.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 1.0
    return sequence, jaccard


def load_captions(path: Path) -> list[str]:
    """Загружает captions из объекта JSON вида {"captions": [12 строк]}."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Не удалось прочитать captions JSON: {path}: {exc}") from exc

    if not isinstance(data, dict) or set(data) - {"description", "captions"} or "captions" not in data:
        raise ValueError("Captions JSON должен быть объектом с полем captions.")
    captions = data["captions"]
    if not isinstance(captions, list):
        raise ValueError("Поле captions должно быть массивом строк.")
    validate_captions(captions)
    return captions


def validate_captions(captions: list[str]) -> None:
    """Проверяет технические признаки различия captions.

    Одинаковая композиция текста разрешена. Эта проверка не определяет смысл:
    редактор вручную подтверждает, что каждая подпись отличается по смыслу и формулировке.
    """
    if len(captions) != CAPTION_COUNT:
        raise ValueError(f"Нужно ровно {CAPTION_COUNT} captions, получено {len(captions)}.")
    if not all(isinstance(caption, str) and caption.strip() for caption in captions):
        raise ValueError("Каждая caption должна быть непустой строкой.")

    normalised = [_normalise_caption(caption) for caption in captions]
    if len(set(normalised)) != CAPTION_COUNT:
        raise ValueError("Найдены точные или нормализованные дубли captions.")

    tag_sets = [_caption_tags(caption) for caption in captions]
    if any(len(tags) < 3 for tags in tag_sets):
        raise ValueError("В каждой caption требуется не менее трёх hashtags.")
    if len(set(tag_sets)) != CAPTION_COUNT:
        raise ValueError("Найдены повторяющиеся полные наборы hashtags.")

    for left_index, left in enumerate(captions):
        for right_index, right in enumerate(captions[left_index + 1:], left_index + 1):
            sequence, jaccard = _caption_similarity(left, right)
            if sequence >= MAX_SEQUENCE_SIMILARITY or jaccard >= MAX_TOKEN_JACCARD:
                raise ValueError(
                    f"Captions {left_index + 1} и {right_index + 1} слишком похожи "
                    f"(SequenceMatcher={sequence:.3f}, token Jaccard={jaccard:.3f})."
                )


def _expected_files(prefix: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise ValueError("prefix может содержать только латинские буквы, цифры, _ и -.")
    return [f"{prefix}_u{index:02d}.mp4" for index in range(1, CAPTION_COUNT + 1)]


def _schedule_items(prefix: str, captions: list[str]) -> list[dict[str, object]]:
    files = _expected_files(prefix)
    items = []
    for zero_index, (file_name, caption) in enumerate(zip(files, captions)):
        day = zero_index // 3 + 1
        slot = zero_index % 3 + 1
        items.append(
            {
                "index": zero_index + 1,
                "file": file_name,
                "day": day,
                "slot": slot,
                "relative_seconds": (day - 1) * 86_400 + SLOT_OFFSETS[slot - 1],
                "caption": caption,
            }
        )
    return items


def _render_html(prefix: str, items: list[dict[str, object]]) -> str:
    rows = []
    for item in items:
        caption = html.escape(str(item["caption"])).replace("\n", "<br>")
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(item['file']))}</code></td>"
            f"<td>День {item['day']} / слот {item['slot']}</td>"
            f"<td>T0 + {int(item['relative_seconds']) // 3600:02d}:{(int(item['relative_seconds']) % 3600) // 60:02d}</td>"
            f"<td class=\"msk-time\" data-offset=\"{item['relative_seconds']}\">Укажите T0</td>"
            f"<td><details><summary>Открыть подпись</summary><div class=\"caption\">{caption}</div></details></td>"
            "</tr>"
        )

    rows_html = "\n".join(rows)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(prefix)} — план публикаций</title>
  <style>
    :root {{ color-scheme: light dark; --bg:#f5f7fb; --card:#fff; --text:#182033; --muted:#5d687d; --line:#dce2ed; --accent:#6558ed; --good:#16855f; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1219; --card:#191e29; --text:#edf1fa; --muted:#aab3c6; --line:#30384a; --accent:#a197ff; --good:#59d7aa; }} }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1280px; margin:auto; padding:30px 20px 64px; }} h1 {{ font-size:32px; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:18px 0; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; }}
    .metric b {{ display:block; font-size:27px; }} .muted,.metric span {{ color:var(--muted); }} .good {{ border-left:5px solid var(--good); }}
    table {{ width:100%; border-collapse:collapse; background:var(--card); }} th,td {{ padding:10px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }}
    th {{ background:color-mix(in srgb,var(--card),var(--accent) 10%); }} code,summary,a {{ color:var(--accent); }} summary {{ cursor:pointer; }}
    .caption {{ min-width:390px; padding-top:8px; white-space:normal; }} input,button {{ padding:9px; border-radius:8px; }}
    input {{ border:1px solid var(--line); background:var(--bg); color:var(--text); }} button {{ border:0; background:var(--accent); color:white; cursor:pointer; }}
    @media (max-width:800px) {{ .grid {{ grid-template-columns:1fr; }} .wrap {{ overflow:auto; }} }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(prefix)} — план публикаций</h1>
    <p class="muted">Рабочая ручная схема: четыре последовательных дня, по три Reel в день. Она не является рекомендацией платформы.</p>
    <div class="grid">
      <div class="card metric"><b>12</b><span>готовых файлов</span></div>
      <div class="card metric"><b>4 × 3</b><span>дня × публикации</span></div>
      <div class="card metric"><b>12 / 12</b><span>подписей для ручной проверки</span></div>
    </div>
    <div class="card good"><b>Проверка текста:</b> пройдены технические фильтры дублей и похожести. Одинаковая структура допустима; различие смысла и формулировок должен подтвердить редактор вручную.</div>
    <div class="card"><label for="t0"><b>T0 первого Reel, Москва (+03:00):</b></label> <input id="t0" type="datetime-local" step="1"> <button id="apply" type="button">Рассчитать даты</button><p id="next" class="muted"></p></div>
    <p>Слоты каждой даты: 00:00, 05:00 и 10:00 относительно T0. Откройте <a href="verification.html">verification.html</a> для общей проверки основного пайплайна.</p>
    <div class="wrap"><table><thead><tr><th>Файл</th><th>День / слот</th><th>Относительно T0</th><th>Дата и время МСК</th><th>Caption</th></tr></thead><tbody>
{rows_html}
    </tbody></table></div>
  </main>
  <script>
    (() => {{
      const input = document.getElementById("t0");
      const formatter = new Intl.DateTimeFormat("ru-RU", {{ timeZone:"Europe/Moscow", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false }});
      const parseMoscow = value => value ? new Date(value + (value.length === 16 ? ":00" : "") + "+03:00") : null;
      const format = date => formatter.format(date).replace(",", "") + " МСК";
      function update() {{
        const start = parseMoscow(input.value);
        if (!start || Number.isNaN(start.getTime())) return;
        const future = [];
        document.querySelectorAll(".msk-time").forEach(cell => {{
          const date = new Date(start.getTime() + Number(cell.dataset.offset) * 1000);
          cell.textContent = format(date);
          if (date > new Date()) future.push({{ date, file:cell.closest("tr").querySelector("code").textContent }});
        }});
        future.sort((left, right) => left.date - right.date);
        document.getElementById("next").textContent = future.length ? "Следующая публикация: " + future[0].file + " — " + format(future[0].date) : "Все рассчитанные публикации уже в прошлом.";
      }}
      document.getElementById("apply").addEventListener("click", update);
      input.addEventListener("change", update);
    }})();
  </script>
</body>
</html>
"""


def build_plan(output_dir: Path, prefix: str, captions: list[str]) -> None:
    """Создаёт posting_plan.json и upload_plan.html рядом с 12 ожидаемыми MP4."""
    output_dir = Path(output_dir)
    validate_captions(captions)
    expected_files = _expected_files(prefix)
    actual_files = sorted(path.name for path in output_dir.glob("*.mp4") if path.is_file())
    if actual_files != expected_files:
        missing = sorted(set(expected_files) - set(actual_files))
        unexpected = sorted(set(actual_files) - set(expected_files))
        details = []
        if missing:
            details.append("нет: " + ", ".join(missing))
        if unexpected:
            details.append("лишние: " + ", ".join(unexpected))
        raise ValueError("Нужны ровно ожидаемые 12 MP4 (" + "; ".join(details) + ").")

    items = _schedule_items(prefix, captions)
    payload = {
        "timezone": "Europe/Moscow",
        "timezone_offset": "+03:00",
        "rule": "12 reels: 4 consecutive days, 3 posts per day",
        "schedule_kind": "editable_manual_schedule_not_platform_recommendation",
        "daily_offsets_seconds": list(SLOT_OFFSETS),
        "captions_unique": True,
        "caption_policy": (
            "Captions are authored manually. Shared structure is allowed, but each caption "
            "needs distinct meaning and wording confirmed by human review. Exact/near-duplicate "
            "thresholds are heuristics, not a semantic guarantee."
        ),
        "items": items,
    }
    (output_dir / "posting_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "upload_plan.html").write_text(_render_html(prefix, items), encoding="utf-8")


def verify_plan(output_dir: Path, captions: list[str]) -> list[str]:
    """Независимо проверяет созданные JSON, HTML, MP4 и переданные captions."""
    output_dir = Path(output_dir)
    errors: list[str] = []
    try:
        validate_captions(captions)
    except ValueError as exc:
        errors.append(f"Входные captions: {exc}")
        return errors

    plan_path = output_dir / "posting_plan.json"
    html_path = output_dir / "upload_plan.html"
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Не удалось прочитать posting_plan.json: {exc}"]

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != CAPTION_COUNT:
        return ["posting_plan.json должен содержать ровно 12 items."]
    if data.get("daily_offsets_seconds") != list(SLOT_OFFSETS):
        errors.append("JSON daily_offsets_seconds не равны 0, 18000, 36000.")
    if data.get("schedule_kind") != "editable_manual_schedule_not_platform_recommendation":
        errors.append("JSON не помечает расписание как редактируемое ручное.")

    first_file = items[0].get("file") if isinstance(items[0], dict) else ""
    match = re.fullmatch(r"(.+)_u01\.mp4", first_file) if isinstance(first_file, str) else None
    if not match:
        return errors + ["Нельзя определить prefix из первого имени файла JSON."]
    prefix = match.group(1)
    expected_items = _schedule_items(prefix, captions)

    for item, expected in zip(items, expected_items):
        index = expected["index"]
        if not isinstance(item, dict):
            errors.append(f"JSON item {index} не является объектом.")
            continue
        for field in ("index", "file", "day", "slot", "relative_seconds", "caption"):
            if item.get(field) != expected[field]:
                errors.append(f"JSON item {index}: неверно поле {field}.")
        file_name = item.get("file")
        if not isinstance(file_name, str) or not (output_dir / file_name).is_file():
            errors.append(f"MP4 отсутствует для JSON item {index}: {file_name!r}.")

    item_files = [item.get("file") for item in items if isinstance(item, dict)]
    if len(set(item_files)) != CAPTION_COUNT:
        errors.append("В JSON есть дубли имён MP4.")
    actual_files = sorted(path.name for path in output_dir.glob("*.mp4") if path.is_file())
    if actual_files != _expected_files(prefix):
        errors.append("Набор MP4 в папке не совпадает с prefix_u01.mp4 … prefix_u12.mp4.")

    try:
        page = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        return errors + [f"Не удалось прочитать upload_plan.html: {exc}"]
    html_captions = re.findall(r'<div class="caption">(.*?)</div>', page, flags=re.DOTALL)
    expected_html_captions = [html.escape(caption).replace("\n", "<br>") for caption in captions]
    if html_captions != expected_html_captions:
        errors.append("HTML содержит не те или не все 12 captions.")
    html_offsets = [int(value) for value in re.findall(r'data-offset="(\d+)"', page)]
    expected_offsets = [int(item["relative_seconds"]) for item in expected_items]
    if html_offsets != expected_offsets:
        errors.append("HTML offsets не совпадают с JSON-расписанием.")
    if 'href="verification.html"' not in page:
        errors.append("HTML не содержит ссылку на verification.html.")
    return errors


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Локальная сборка плана публикации 12 Reel.")
    parser.add_argument("output", type=Path, help="Папка с 12 MP4")
    parser.add_argument("--captions", required=True, type=Path, help="JSON с 12 captions")
    parser.add_argument("--prefix", default="010", help="Префикс имён MP4 (по умолчанию: 010)")
    parser.add_argument("--verify", action="store_true", help="Только проверить существующий план")
    args = parser.parse_args(argv)

    try:
        captions = load_captions(args.captions)
        if not args.verify:
            build_plan(args.output, args.prefix, captions)
        errors = verify_plan(args.output, captions)
    except ValueError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1
    print("Проверка плана пройдена.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
