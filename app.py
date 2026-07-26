import html
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import streamlit as st
import streamlit.components.v1 as components
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from yt_dlp import YoutubeDL

st.set_page_config(
    page_title="YouTube 同步中文字幕",
    page_icon="🎬",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {max-width: 1180px; padding-top: 1.3rem;}
    [data-testid="stAppViewContainer"] {background: #0e1117;}
    .app-title {font-size: 2rem; font-weight: 800; margin-bottom: .2rem;}
    .app-subtitle {color: #aab2bf; margin-bottom: 1rem;}
    .status-box {
        border: 1px solid #303846;
        border-radius: 12px;
        padding: .75rem 1rem;
        background: #151a22;
        margin-bottom: .8rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

CHINESE_CODES = ["zh-Hant", "zh-TW", "zh-HK", "zh", "zh-Hans", "zh-CN"]
ENGLISH_CODES = ["en", "en-US", "en-GB", "en-CA", "en-AU"]


def extract_video_id(value: str) -> str | None:
    """支援一般 YouTube、YouTube Music、短網址、Shorts 與直接影片 ID。"""
    value = value.strip()

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value

    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().replace("www.", "")

        if host == "youtu.be":
            candidate = parsed.path.strip("/").split("/")[0]
            return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None

        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                candidate = parse_qs(parsed.query).get("v", [None])[0]
                return candidate if candidate and re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None

            for prefix in ("/shorts/", "/embed/", "/live/"):
                if parsed.path.startswith(prefix):
                    candidate = parsed.path[len(prefix):].split("/")[0]
                    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None
    except Exception:
        return None

    return None


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\n", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_subtitles(items: list[dict]) -> list[dict]:
    result: list[dict] = []
    for item in items:
        text = clean_text(str(item.get("text", "")))
        if not text:
            continue
        start = max(float(item.get("start", 0)), 0)
        duration = max(float(item.get("duration", 0.15)), 0.15)
        result.append(
            {
                "start": round(start, 3),
                "duration": round(duration, 3),
                "end": round(start + duration, 3),
                "text": text,
            }
        )
    return sorted(result, key=lambda x: x["start"])


def transcript_items_to_dicts(fetched) -> list[dict]:
    return normalize_subtitles(
        [
            {
                "start": float(item.start),
                "duration": float(item.duration),
                "text": item.text,
            }
            for item in fetched
        ]
    )


def find_existing_transcript(transcript_list, codes: list[str]):
    try:
        return transcript_list.find_manually_created_transcript(codes)
    except Exception:
        pass
    try:
        return transcript_list.find_generated_transcript(codes)
    except Exception:
        pass
    return None


def pick_translation_code(transcript) -> str | None:
    available = {item.language_code for item in transcript.translation_languages}
    for code in CHINESE_CODES:
        if code in available:
            return code
    return None


def load_with_transcript_api(video_id: str) -> tuple[list[dict], str]:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)

    existing_chinese = find_existing_transcript(transcript_list, CHINESE_CODES)
    if existing_chinese:
        kind = "自動產生" if existing_chinese.is_generated else "人工建立"
        return (
            transcript_items_to_dicts(existing_chinese.fetch()),
            f"使用影片現有中文字幕（{kind}｜{existing_chinese.language}）",
        )

    source = find_existing_transcript(transcript_list, ENGLISH_CODES)
    if source is None:
        candidates = list(transcript_list)
        source = next((x for x in candidates if x.is_translatable), None)

    if source is None:
        raise NoTranscriptFound(video_id, CHINESE_CODES + ENGLISH_CODES, transcript_list)

    if not source.is_translatable:
        raise RuntimeError(f"找到「{source.language}」字幕，但 YouTube 沒有提供中文翻譯。")

    target_code = pick_translation_code(source)
    if not target_code:
        raise RuntimeError("YouTube 目前沒有提供這部影片的中文翻譯選項。")

    translated = source.translate(target_code)
    kind = "自動辨識" if source.is_generated else "人工字幕"
    return (
        transcript_items_to_dicts(translated.fetch()),
        f"由 {source.language} {kind}翻譯為中文",
    )


def parse_vtt_time(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        hours, minutes, seconds = "0", parts[0], parts[1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_vtt(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore").replace("\r", "")
    blocks = re.split(r"\n\s*\n", raw)
    items: list[dict] = []

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue

        timing = lines[timing_index].split("-->")
        if len(timing) != 2:
            continue

        try:
            start = parse_vtt_time(timing[0].strip().split()[0])
            end = parse_vtt_time(timing[1].strip().split()[0])
        except Exception:
            continue

        text = clean_text(" ".join(lines[timing_index + 1 :]))
        text = re.sub(r"<\d\d:\d\d(?::\d\d)?\.\d+>", "", text)
        text = re.sub(r"^\s*[-–]\s*", "", text)
        if text:
            items.append({"start": start, "duration": max(end - start, 0.15), "text": text})

    # YouTube 自動字幕常有重複累加內容，清理連續重複句。
    cleaned: list[dict] = []
    for item in normalize_subtitles(items):
        if cleaned and item["text"] == cleaned[-1]["text"]:
            cleaned[-1]["end"] = max(cleaned[-1]["end"], item["end"])
            cleaned[-1]["duration"] = cleaned[-1]["end"] - cleaned[-1]["start"]
        else:
            cleaned.append(item)
    return cleaned


def download_youtube_subtitles(video_url: str, workdir: Path) -> tuple[list[dict], str] | None:
    output_template = str(workdir / "subtitle.%(ext)s")
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": CHINESE_CODES + ENGLISH_CODES,
        "subtitlesformat": "vtt",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    files = list(workdir.glob("subtitle*.vtt"))
    if not files:
        return None

    def priority(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        for index, code in enumerate(CHINESE_CODES):
            if f".{code.lower()}." in name:
                return index, name
        for index, code in enumerate(ENGLISH_CODES, start=20):
            if f".{code.lower()}." in name:
                return index, name
        return 99, name

    selected = sorted(files, key=priority)[0]
    subtitles = parse_vtt(selected)
    if not subtitles:
        return None

    selected_name = selected.name.lower()
    is_chinese = any(f".{code.lower()}." in selected_name for code in CHINESE_CODES)
    language = "中文" if is_chinese else "外語"
    title = info.get("title") or "YouTube 影片"
    return subtitles, f"透過 yt-dlp 取得影片{language}字幕｜{title}"


def get_whisper_model(model_size: str) -> WhisperModel:
    cache_key = f"whisper_model_{model_size}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
        )
    return st.session_state[cache_key]


def download_audio(video_url: str, workdir: Path) -> tuple[Path, str]:
    output_template = str(workdir / "audio.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    audio_path = workdir / "audio.mp3"
    if not audio_path.exists():
        candidates = list(workdir.glob("audio.*"))
        if not candidates:
            raise RuntimeError("無法取得影片音訊。")
        audio_path = candidates[0]

    return audio_path, info.get("title") or "YouTube 影片"


def translate_texts_to_chinese(texts: list[str], progress=None) -> list[str]:
    translator = GoogleTranslator(source="auto", target="zh-TW")
    translated: list[str] = []
    total = max(len(texts), 1)

    for index, text in enumerate(texts):
        try:
            translated_text = translator.translate(text)
            translated.append(clean_text(translated_text) or text)
        except Exception:
            translated.append(text)

        if progress is not None:
            progress.progress((index + 1) / total, text=f"正在翻譯中文字幕：{index + 1}/{total}")

    return translated


def transcribe_audio_to_chinese(
    audio_path: Path,
    model_size: str,
    progress=None,
) -> tuple[list[dict], str]:
    model = get_whisper_model(model_size)
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True,
    )

    raw_segments: list[dict] = []
    for segment in segments:
        text = clean_text(segment.text)
        if text:
            raw_segments.append(
                {
                    "start": float(segment.start),
                    "duration": max(float(segment.end - segment.start), 0.15),
                    "text": text,
                }
            )

    if not raw_segments:
        raise RuntimeError("語音辨識完成，但沒有辨識到可用文字。")

    language = (info.language or "unknown").lower()
    if language.startswith("zh"):
        return normalize_subtitles(raw_segments), "Whisper 語音辨識（原語言為中文）"

    translated = translate_texts_to_chinese([item["text"] for item in raw_segments], progress)
    for item, zh_text in zip(raw_segments, translated):
        item["text"] = zh_text

    return normalize_subtitles(raw_segments), f"Whisper 語音辨識並翻譯為中文（偵測語言：{language}）"


def load_subtitles_with_fallback(
    video_id: str,
    model_size: str,
    allow_speech_recognition: bool,
    status,
    progress,
) -> tuple[list[dict], str]:
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    errors: list[str] = []

    status.write("① 正在嘗試讀取 YouTube 現有字幕……")
    try:
        subtitles, note = load_with_transcript_api(video_id)
        if subtitles:
            progress.progress(1.0, text="字幕處理完成")
            return subtitles, note
    except Exception as exc:
        errors.append(f"字幕 API：{exc}")

    with tempfile.TemporaryDirectory(prefix="yt_caption_") as tmp:
        workdir = Path(tmp)

        status.write("② 字幕 API 無法使用，改用 yt-dlp 尋找字幕……")
        try:
            result = download_youtube_subtitles(video_url, workdir)
            if result:
                subtitles, note = result
                # 非中文字字幕再逐句翻譯。
                selected_note = note
                if "外語字幕" in note:
                    translated = translate_texts_to_chinese(
                        [item["text"] for item in subtitles],
                        progress,
                    )
                    for item, zh_text in zip(subtitles, translated):
                        item["text"] = zh_text
                    selected_note += "，並翻譯為繁體中文"
                progress.progress(1.0, text="字幕處理完成")
                return subtitles, selected_note
        except Exception as exc:
            errors.append(f"yt-dlp 字幕：{exc}")

        if not allow_speech_recognition:
            raise RuntimeError("找不到可用字幕，而且你沒有開啟語音辨識。")

        if shutil.which("ffmpeg") is None:
            raise RuntimeError("伺服器尚未安裝 FFmpeg，無法啟用語音辨識。")

        status.write("③ 找不到可用字幕，正在下載暫存音訊並啟動語音辨識……")
        try:
            progress.progress(0.05, text="正在取得影片音訊")
            audio_path, title = download_audio(video_url, workdir)
            progress.progress(0.15, text="正在載入 Whisper 語音模型")
            subtitles, note = transcribe_audio_to_chinese(audio_path, model_size, progress)
            progress.progress(1.0, text="語音辨識完成")
            return subtitles, f"{note}｜{title}"
        except Exception as exc:
            errors.append(f"語音辨識：{exc}")

    raise RuntimeError("所有字幕取得方式都失敗。\n\n" + "\n".join(errors))


def seconds_to_srt_time(seconds: float) -> str:
    milliseconds = int(round(max(seconds, 0) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def subtitles_to_srt(subtitles: list[dict]) -> str:
    blocks = []
    for index, item in enumerate(subtitles, start=1):
        blocks.append(
            f"{index}\n{seconds_to_srt_time(item['start'])} --> "
            f"{seconds_to_srt_time(item['end'])}\n{item['text']}"
        )
    return "\n\n".join(blocks) + "\n"


def subtitles_to_txt(subtitles: list[dict]) -> str:
    return "\n".join(
        f"[{seconds_to_srt_time(item['start']).replace(',', '.')[:-4]}] {item['text']}"
        for item in subtitles
    )


def build_player(video_id: str, subtitles: list[dict]) -> str:
    subtitle_json = json.dumps(subtitles, ensure_ascii=False).replace("</", "<\\/")

    return f"""
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #0e1117; color: #f7f8fa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft JhengHei", sans-serif; }}
.grid {{ display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(330px, .85fr); gap: 14px; height: 790px; }}
.panel {{ background: #151a22; border: 1px solid #303846; border-radius: 14px; overflow: hidden; }}
.video-wrap {{ position: relative; width: 100%; aspect-ratio: 16 / 9; background: black; }}
#player {{ width: 100%; height: 100%; }}
.caption-overlay {{ position: absolute; left: 5%; right: 5%; bottom: 5%; z-index: 10; text-align: center; pointer-events: none; }}
.caption-overlay span {{ display: inline-block; max-width: 100%; padding: 8px 13px; border-radius: 8px; background: rgba(0,0,0,.78); font-size: clamp(18px, 2.1vw, 30px); font-weight: 750; line-height: 1.45; text-shadow: 0 2px 3px #000; }}
.controls {{ display: flex; flex-wrap: wrap; align-items: center; gap: 9px; padding: 12px; border-top: 1px solid #303846; }}
button {{ border: 1px solid #394353; background: #202733; color: white; padding: 8px 12px; border-radius: 9px; cursor: pointer; }}
button:hover {{ background: #2b3442; }}
#time {{ color: #aab2bf; font-variant-numeric: tabular-nums; }}
.transcript-panel {{ display: flex; flex-direction: column; min-height: 0; }}
.header {{ padding: 13px 15px; border-bottom: 1px solid #303846; font-weight: 800; }}
#transcript {{ overflow-y: auto; padding: 9px; scroll-behavior: smooth; }}
.line {{ display: grid; grid-template-columns: 58px 1fr; gap: 8px; padding: 10px 9px; margin-bottom: 5px; border-radius: 9px; cursor: pointer; border: 1px solid transparent; line-height: 1.55; }}
.line:hover {{ background: #202733; }}
.line.active {{ background: #273851; border-color: #4e7ebc; }}
.timestamp {{ color: #82aef0; font-size: 13px; padding-top: 3px; font-variant-numeric: tabular-nums; }}
.text {{ font-size: 16px; }}
@media (max-width: 850px) {{
    .grid {{ display: block; height: auto; }}
    .transcript-panel {{ height: 430px; margin-top: 14px; }}
}}
</style>
</head>
<body>
<div class="grid">
    <section class="panel">
        <div class="video-wrap">
            <div id="player"></div>
            <div class="caption-overlay"><span id="overlay">準備播放</span></div>
        </div>
        <div class="controls">
            <button onclick="jumpRelative(-5)">−5 秒</button>
            <button onclick="jumpRelative(5)">+5 秒</button>
            <button onclick="toggleOverlay()">顯示／隱藏影片字幕</button>
            <span id="time">00:00</span>
        </div>
    </section>
    <section class="panel transcript-panel">
        <div class="header">同步中文字幕稿｜點擊句子可跳轉</div>
        <div id="transcript"></div>
    </section>
</div>
<script>
const subtitles = {subtitle_json};
let player = null;
let currentIndex = -1;
let overlayVisible = true;
function formatTime(seconds) {{
    seconds = Math.max(0, Math.floor(seconds || 0));
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return String(minutes).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
}}
function renderTranscript() {{
    const container = document.getElementById("transcript");
    subtitles.forEach((item, index) => {{
        const row = document.createElement("div");
        row.className = "line";
        row.id = "line-" + index;
        row.onclick = () => {{ if (player && player.seekTo) {{ player.seekTo(item.start, true); player.playVideo(); }} }};
        const timestamp = document.createElement("div");
        timestamp.className = "timestamp";
        timestamp.textContent = formatTime(item.start);
        const text = document.createElement("div");
        text.className = "text";
        text.textContent = item.text;
        row.appendChild(timestamp); row.appendChild(text); container.appendChild(row);
    }});
}}
function findSubtitleIndex(time) {{
    let low = 0, high = subtitles.length - 1, answer = -1;
    while (low <= high) {{
        const mid = Math.floor((low + high) / 2);
        if (subtitles[mid].start <= time) {{ answer = mid; low = mid + 1; }} else {{ high = mid - 1; }}
    }}
    if (answer < 0) return -1;
    const item = subtitles[answer];
    const nextStart = answer + 1 < subtitles.length ? subtitles[answer + 1].start : item.end + 2;
    return time < Math.max(item.end, nextStart) ? answer : -1;
}}
function updateSubtitle() {{
    if (!player || !player.getCurrentTime) return;
    const time = player.getCurrentTime();
    document.getElementById("time").textContent = formatTime(time);
    const index = findSubtitleIndex(time);
    if (index === currentIndex) return;
    if (currentIndex >= 0) {{ const oldLine = document.getElementById("line-" + currentIndex); if (oldLine) oldLine.classList.remove("active"); }}
    currentIndex = index;
    const overlay = document.getElementById("overlay");
    if (index >= 0) {{
        const activeLine = document.getElementById("line-" + index);
        if (activeLine) {{ activeLine.classList.add("active"); activeLine.scrollIntoView({{ behavior: "smooth", block: "center" }}); }}
        overlay.textContent = subtitles[index].text;
    }} else {{ overlay.textContent = ""; }}
}}
function jumpRelative(seconds) {{ if (player && player.getCurrentTime) player.seekTo(Math.max(0, player.getCurrentTime() + seconds), true); }}
function toggleOverlay() {{ overlayVisible = !overlayVisible; document.querySelector(".caption-overlay").style.display = overlayVisible ? "block" : "none"; }}
renderTranscript();
const tag = document.createElement("script"); tag.src = "https://www.youtube.com/iframe_api"; document.head.appendChild(tag);
function onYouTubeIframeAPIReady() {{
    player = new YT.Player("player", {{
        videoId: "{video_id}",
        playerVars: {{ autoplay: 0, controls: 1, rel: 0, playsinline: 1, cc_load_policy: 0 }},
        events: {{ onReady: () => {{ document.getElementById("overlay").textContent = "按播放開始"; setInterval(updateSubtitle, 180); }} }}
    }});
}}
</script>
</body>
</html>
"""


st.markdown('<div class="app-title">🎬 YouTube 同步中文字幕</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="app-subtitle">有字幕就直接翻譯；沒有字幕時，可改用 Whisper 語音辨識產生中文字幕。</div>',
    unsafe_allow_html=True,
)

url = st.text_input(
    "YouTube 網址",
    placeholder="例如：https://music.youtube.com/watch?v=xxxxxxxxxxx",
)

with st.expander("辨識設定", expanded=False):
    allow_speech_recognition = st.checkbox(
        "找不到字幕時，自動使用語音辨識",
        value=True,
        help="需要下載暫存音訊，處理時間取決於影片長度與伺服器效能。",
    )
    model_label = st.selectbox(
        "Whisper 模型",
        ["tiny（最快、準確度較低）", "base（建議）", "small（較準確、較慢）"],
        index=1,
    )
    model_size = {"tiny（最快、準確度較低）": "tiny", "base（建議）": "base", "small（較準確、較慢）": "small"}[model_label]

col1, col2 = st.columns([1, 4])
with col1:
    load_button = st.button("讀取／產生中文字幕", type="primary", use_container_width=True)
with col2:
    st.caption("處理順序：影片現有字幕 → yt-dlp 字幕 → Whisper 語音辨識。")

if load_button:
    video_id = extract_video_id(url)
    if not video_id:
        st.error("網址格式不正確，請貼上完整 YouTube／YouTube Music 網址。")
    else:
        status = st.status("正在處理影片……", expanded=True)
        progress = st.progress(0.0, text="準備開始")
        try:
            subtitles, source_note = load_subtitles_with_fallback(
                video_id,
                model_size,
                allow_speech_recognition,
                status,
                progress,
            )
            if not subtitles:
                raise RuntimeError("字幕內容是空的，無法顯示。")

            st.session_state["video_id"] = video_id
            st.session_state["subtitles"] = subtitles
            st.session_state["source_note"] = source_note
            status.update(label="中文字幕已完成", state="complete", expanded=False)
        except TranscriptsDisabled:
            status.update(label="影片字幕已關閉", state="error")
            st.error("這部影片已關閉字幕，而且語音辨識沒有成功完成。")
        except VideoUnavailable:
            status.update(label="影片無法使用", state="error")
            st.error("影片不存在、私人影片、年齡限制，或目前無法播放。")
        except (NoTranscriptFound, CouldNotRetrieveTranscript) as exc:
            status.update(label="無法取得字幕", state="error")
            st.error(f"無法取得字幕：{exc}")
        except Exception as exc:
            status.update(label="處理失敗", state="error")
            st.error(str(exc))

if "video_id" in st.session_state and "subtitles" in st.session_state:
    subtitles = st.session_state["subtitles"]
    st.markdown(
        f'<div class="status-box">✅ {html.escape(st.session_state["source_note"])}｜共 {len(subtitles)} 段字幕</div>',
        unsafe_allow_html=True,
    )

    download_col1, download_col2, _ = st.columns([1, 1, 3])
    with download_col1:
        st.download_button(
            "下載 SRT 字幕",
            data=subtitles_to_srt(subtitles).encode("utf-8-sig"),
            file_name=f"{st.session_state['video_id']}_zh-TW.srt",
            mime="application/x-subrip",
            use_container_width=True,
        )
    with download_col2:
        st.download_button(
            "下載 TXT 字幕稿",
            data=subtitles_to_txt(subtitles).encode("utf-8-sig"),
            file_name=f"{st.session_state['video_id']}_zh-TW.txt",
            mime="text/plain",
            use_container_width=True,
        )

    components.html(
        build_player(st.session_state["video_id"], subtitles),
        height=820,
        scrolling=False,
    )

    with st.expander("限制與準確度說明"):
        st.write(
            "字幕時間會依照 YouTube 字幕或 Whisper 語音片段同步。歌曲、背景音樂過大、多人同時說話、口音與專有名詞，都可能造成辨識或翻譯誤差。"
        )
        st.write(
            "語音辨識模式會暫時取得音訊，完成後自動刪除；影片仍由 YouTube 官方播放器播放。"
        )
        st.warning(
            "Streamlit 免費雲端的 IP 仍可能遭 YouTube 限制。若字幕與音訊都無法取得，需改成本機執行或部署到其他伺服器。"
        )
