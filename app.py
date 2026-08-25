import html
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import streamlit as st
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel
from yt_dlp import YoutubeDL

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError:
    YouTubeTranscriptApi = None
    GenericProxyConfig = None

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

TRANSLATION_ERROR_MARKERS = (
    "error 500",
    "server error",
    "that's an error",
    "that’s an error",
    "there was an error",
    "please try again later",
    "that's all we know",
    "that’s all we know",
)

# 極保守模式：以降低 Streamlit 共用 IP 被 YouTube 429/403 限制為第一優先。
YOUTUBE_COOLDOWN_SECONDS = 20 * 60
YOUTUBE_STEP_DELAY_SECONDS = 12
YOUTUBE_AUDIO_DELAY_SECONDS = 20
YOUTUBE_BLOCK_MARKERS = (
    "429",
    "too many requests",
    "403",
    "forbidden",
    "temporarily blocked",
    "request blocked",
)


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


def is_translation_error_response(text: str) -> bool:
    """避免把翻譯服務的 500/錯誤頁文字誤當成字幕。"""
    normalized = clean_text(text).lower()
    return any(marker in normalized for marker in TRANSLATION_ERROR_MARKERS)


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

    cleaned: list[dict] = []
    for item in normalize_subtitles(items):
        if cleaned and item["text"] == cleaned[-1]["text"]:
            cleaned[-1]["end"] = max(cleaned[-1]["end"], item["end"])
            cleaned[-1]["duration"] = cleaned[-1]["end"] - cleaned[-1]["start"]
        else:
            cleaned.append(item)
    return cleaned


def get_optional_secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
        return str(value).strip() if value else None
    except Exception:
        return None


def is_youtube_block_error(error: Exception | str) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in YOUTUBE_BLOCK_MARKERS)


def activate_youtube_cooldown() -> None:
    st.session_state["youtube_cooldown_until"] = time.time() + YOUTUBE_COOLDOWN_SECONDS


def youtube_cooldown_remaining() -> int:
    until = float(st.session_state.get("youtube_cooldown_until", 0) or 0)
    return max(0, int(until - time.time()))


def format_wait(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def get_ytdlp_common_options(
    cookie_path: Path | None = None,
    player_clients: list[str] | None = None,
) -> dict:
    """極保守 yt-dlp 設定：不連續重試、單線下載、每次請求主動降速。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # 不讓 yt-dlp 在背景自己連續轟同一個 YouTube IP。
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "file_access_retries": 0,
        "socket_timeout": 45,
        "continuedl": True,
        "concurrent_fragment_downloads": 1,
        # yt-dlp 自己也額外放慢請求節奏。
        "sleep_interval_requests": 5,
        "sleep_interval": 10,
        "max_sleep_interval": 15,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "zh-TW,zh-Hant;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    }

    # 極保守模式原則上不主動切換多個 player client。
    # 僅在呼叫端明確指定時才設定，避免一次失敗後連續換身份請求。
    if player_clients:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": player_clients,
                "fetch_pot": ["auto"],
            }
        }

    if cookie_path and cookie_path.exists():
        opts["cookiefile"] = str(cookie_path.resolve())
    else:
        local_cookie = Path("cookies.txt")
        if local_cookie.exists() and local_cookie.is_file():
            opts["cookiefile"] = str(local_cookie.resolve())

    proxy_url = get_optional_secret("YOUTUBE_PROXY")
    if proxy_url:
        opts["proxy"] = proxy_url

    return opts


def translate_texts_to_chinese(texts: list[str], progress=None) -> list[str]:
    """逐句翻譯；遇到 500/錯誤頁自動重試，最後保留原文而不是顯示錯誤訊息。"""
    translator = GoogleTranslator(source="auto", target="zh-TW")
    translated: list[str] = []
    total = max(len(texts), 1)

    for index, text in enumerate(texts):
        source_text = clean_text(text)
        final_text = source_text

        for attempt in range(3):
            try:
                candidate = clean_text(translator.translate(source_text))
                if candidate and not is_translation_error_response(candidate):
                    final_text = candidate
                    break
            except Exception:
                pass

            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))

        translated.append(final_text)

        if progress is not None:
            progress.progress(
                (index + 1) / total,
                text=f"正在翻譯中文字幕：{index + 1}/{total}",
            )

    return translated


def download_transcript_api(
    video_id: str,
    proxy_url: str | None = None,
) -> tuple[list[dict], str] | None:
    if YouTubeTranscriptApi is None:
        return None

    kwargs = {}
    if proxy_url and GenericProxyConfig is not None:
        kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)

    api = YouTubeTranscriptApi(**kwargs)
    transcript_list = api.list(video_id)

    selected = None
    for code in CHINESE_CODES + ENGLISH_CODES:
        try:
            selected = transcript_list.find_transcript([code])
            break
        except Exception:
            continue

    if selected is None:
        for transcript in transcript_list:
            selected = transcript
            break

    if selected is None:
        return None

    subtitles = normalize_subtitles(selected.fetch().to_raw_data())
    if not subtitles:
        return None

    language_code = str(getattr(selected, "language_code", "")).lower()
    note = f"YouTube Transcript API 取得{getattr(selected, 'language', '字幕')}"

    if not language_code.startswith("zh"):
        translated = translate_texts_to_chinese([item["text"] for item in subtitles])
        for item, zh_text in zip(subtitles, translated):
            item["text"] = zh_text
        note += "，並翻譯為繁體中文"

    return subtitles, note


def download_youtube_subtitles(
    video_url: str,
    workdir: Path,
    cookie_path: Path | None = None,
) -> tuple[list[dict], str] | None:
    """極保守字幕擷取：單一策略、單一語言、低頻率，不做連續客戶端切換。"""
    # 只保留最常用的三個候選，避免一次要求一大串語言。
    language_attempts = [
        ("繁體中文", "zh-Hant"),
        ("台灣中文", "zh-TW"),
        ("英文", "en"),
    ]
    last_error = None

    for attempt_index, (language_name, language_code) in enumerate(language_attempts):
        output_template = str(workdir / f"subtitle_{language_code}.%(ext)s")
        opts = get_ytdlp_common_options(cookie_path, None)
        opts.update(
            {
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [language_code],
                "subtitlesformat": "vtt",
                "outtmpl": output_template,
            }
        )

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
        except Exception as exc:
            # 429 / 403 不再嘗試下一個語言，立刻往上拋，讓整個流程冷卻。
            if is_youtube_block_error(exc):
                raise
            last_error = exc
            info = {}

        files = list(workdir.glob(f"subtitle_{language_code}*.vtt"))
        if files:
            selected = sorted(files)[0]
            subtitles = parse_vtt(selected)
            if subtitles:
                is_chinese = language_code.lower().startswith("zh")
                language = "中文" if is_chinese else "外語"
                title = info.get("title") or "YouTube 影片"
                return subtitles, f"透過 yt-dlp 極保守模式取得影片{language}字幕（{language_name}）｜{title}"

        # 下一個候選前固定等待，絕不連續轟炸。
        if attempt_index < len(language_attempts) - 1:
            time.sleep(YOUTUBE_STEP_DELAY_SECONDS)

    if last_error:
        raise last_error
    return None


def get_whisper_model(model_size: str) -> WhisperModel:
    cache_key = f"whisper_model_{model_size}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return st.session_state[cache_key]


def download_audio(video_url: str, workdir: Path, cookie_path: Path | None = None) -> tuple[Path, str]:
    """極保守音訊下載：只嘗試一次，不切換多個 YouTube player client。"""
    output_template = str(workdir / "audio.%(ext)s")
    opts = get_ytdlp_common_options(cookie_path, None)
    opts.update(
        {
            "format": "bestaudio[abr<=128]/bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "96",
                }
            ],
        }
    )

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    audio_path = workdir / "audio.mp3"
    if not audio_path.exists():
        candidates = list(workdir.glob("audio.*"))
        if not candidates:
            raise RuntimeError("無法取得影片音訊。")
        audio_path = candidates[0]

    return audio_path, info.get("title") or "YouTube 影片"


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
    cookie_bytes: bytes | None = None,
) -> tuple[list[dict], str]:
    """極保守流程：任何 429/403 立即停止，避免 fallback 連續製造更多 YouTube 請求。"""
    # 同一個瀏覽器工作階段已成功處理過的影片直接用快取，不再打 YouTube。
    result_cache = st.session_state.setdefault("subtitle_result_cache", {})
    if video_id in result_cache:
        cached_subtitles, cached_note = result_cache[video_id]
        progress.progress(1.0, text="使用已完成的字幕快取")
        return cached_subtitles, f"{cached_note}｜工作階段快取"

    remaining = youtube_cooldown_remaining()
    if remaining > 0:
        raise RuntimeError(
            "目前處於 YouTube 保護冷卻期。為避免 Streamlit 共用 IP 被封鎖，"
            f"暫停所有 YouTube 擷取，請約 {format_wait(remaining)} 後再試。"
        )

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    errors: list[str] = []

    def save_and_return(subtitles: list[dict], note: str):
        result_cache[video_id] = (subtitles, note)
        return subtitles, note

    with tempfile.TemporaryDirectory(prefix="yt_caption_") as tmp:
        workdir = Path(tmp)
        cookie_path = None
        if cookie_bytes:
            cookie_path = workdir / "cookies.txt"
            cookie_path.write_bytes(cookie_bytes)

        proxy_url = get_optional_secret("YOUTUBE_PROXY")

        # ① 最省流量：Transcript API，只試一次。
        status.write("① 極保守模式：只嘗試一次字幕 API……")
        try:
            result = download_transcript_api(video_id, proxy_url)
            if result:
                subtitles, note = result
                progress.progress(1.0, text="字幕處理完成")
                return save_and_return(subtitles, note)
        except Exception as exc:
            if is_youtube_block_error(exc):
                activate_youtube_cooldown()
                raise RuntimeError(
                    "YouTube 已回傳 429/403。極保守模式已立即停止，不再接著跑 yt-dlp 或 Whisper。"
                    "已啟動 20 分鐘冷卻，避免共用 IP 被進一步限制。"
                ) from exc
            errors.append(f"Transcript API：{exc}")

        status.write(
            f"字幕 API 未成功。為降低請求頻率，等待 {YOUTUBE_STEP_DELAY_SECONDS} 秒後才嘗試 yt-dlp……"
        )
        time.sleep(YOUTUBE_STEP_DELAY_SECONDS)

        # ② yt-dlp：只用一種策略、一次一個語言。
        status.write("② 極保守 yt-dlp：單一策略、單語言依序嘗試……")
        try:
            result = download_youtube_subtitles(video_url, workdir, cookie_path)
            if result:
                subtitles, note = result
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
                return save_and_return(subtitles, selected_note)
        except Exception as exc:
            if is_youtube_block_error(exc):
                activate_youtube_cooldown()
                raise RuntimeError(
                    "YouTube 已回傳 429/403。極保守模式已停止所有後續下載，"
                    "不會再切換策略或接著抓音訊。已啟動 20 分鐘冷卻。"
                ) from exc
            errors.append(f"yt-dlp 字幕：{exc}")

        # 只有「沒有字幕且沒有被 429/403 擋」時才考慮 Whisper。
        if not allow_speech_recognition:
            raise RuntimeError(
                "目前沒有取得可用字幕。極保守模式預設不自動下載整段音訊，"
                "避免增加 YouTube 請求。若確定需要，可在「辨識設定」手動開啟語音辨識。"
                + ("\n\n" + "\n".join(errors) if errors else "")
            )

        if shutil.which("ffmpeg") is None:
            raise RuntimeError("伺服器尚未安裝 FFmpeg，無法啟用語音辨識。")

        status.write(
            f"③ 已確認不是 429/403。為保守起見，再等待 {YOUTUBE_AUDIO_DELAY_SECONDS} 秒後只嘗試一次音訊下載……"
        )
        time.sleep(YOUTUBE_AUDIO_DELAY_SECONDS)

        try:
            progress.progress(0.05, text="極保守模式：只嘗試一次影片音訊")
            audio_path, title = download_audio(video_url, workdir, cookie_path)
            progress.progress(0.15, text="正在載入 Whisper 語音模型")
            subtitles, note = transcribe_audio_to_chinese(audio_path, model_size, progress)
            progress.progress(1.0, text="語音辨識完成")
            final_note = f"{note}｜{title}"
            return save_and_return(subtitles, final_note)
        except Exception as exc:
            if is_youtube_block_error(exc):
                activate_youtube_cooldown()
                raise RuntimeError(
                    "音訊下載時 YouTube 回傳 429/403。已立即停止並啟動 20 分鐘冷卻。"
                ) from exc
            errors.append(f"語音辨識：{exc}")

    raise RuntimeError(
        "所有保守取得方式都失敗。系統沒有進行高頻率重試。"
        "\n\n" + "\n".join(errors)
    )


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
#transcript {{ overflow-y: auto; padding: 9px; scroll-behavior: smooth; overscroll-behavior: contain; position: relative; min-height: 0; }}
.line {{ display: grid; grid-template-columns: 58px 1fr; gap: 8px; padding: 10px 9px; margin-bottom: 5px; border-radius: 9px; cursor: pointer; border: 1px solid transparent; line-height: 1.55; }}
.line:hover {{ background: #202733; }}
.line.active {{ background: #273851; border-color: #4e7ebc; }}
.timestamp {{ color: #82aef0; font-size: 13px; padding-top: 3px; font-variant-numeric: tabular-nums; }}
.text {{ font-size: 16px; }}
@media (max-width: 850px) and (orientation: portrait) {{
    .grid {{ grid-template-columns: 1fr; grid-template-rows: auto minmax(320px, 46vh); height: auto; gap: 12px; }}
    .transcript-panel {{ height: min(46vh, 430px); min-height: 320px; margin-top: 0; }}
}}
@media (max-width: 950px) and (orientation: landscape) {{
    .grid {{ grid-template-columns: minmax(0, 1.35fr) minmax(280px, .9fr); grid-template-rows: 1fr; height: calc(100vh - 18px); min-height: 390px; gap: 10px; }}
    .transcript-panel {{ height: 100%; min-height: 0; margin-top: 0; }}
    .caption-overlay span {{ font-size: clamp(15px, 2vw, 23px); }}
    .controls {{ padding: 8px; gap: 6px; }}
    button {{ padding: 7px 9px; }}
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
        row.appendChild(timestamp);
        row.appendChild(text);
        container.appendChild(row);
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

    if (currentIndex >= 0) {{
        const oldLine = document.getElementById("line-" + currentIndex);
        if (oldLine) oldLine.classList.remove("active");
    }}

    currentIndex = index;
    const overlay = document.getElementById("overlay");
    if (index >= 0) {{
        const activeLine = document.getElementById("line-" + index);
        if (activeLine) {{
            activeLine.classList.add("active");
            const transcript = document.getElementById("transcript");
            const targetTop = activeLine.offsetTop - (transcript.clientHeight / 2) + (activeLine.offsetHeight / 2);
            transcript.scrollTo({{ top: Math.max(0, targetTop), behavior: "smooth" }});
        }}
        overlay.textContent = subtitles[index].text;
    }} else {{
        overlay.textContent = "";
    }}
}}

function jumpRelative(seconds) {{
    if (player && player.getCurrentTime) player.seekTo(Math.max(0, player.getCurrentTime() + seconds), true);
}}

function toggleOverlay() {{
    overlayVisible = !overlayVisible;
    document.querySelector(".caption-overlay").style.display = overlayVisible ? "block" : "none";
}}

renderTranscript();
const tag = document.createElement("script");
tag.src = "https://www.youtube.com/iframe_api";
document.head.appendChild(tag);

function onYouTubeIframeAPIReady() {{
    player = new YT.Player("player", {{
        videoId: "{video_id}",
        playerVars: {{ autoplay: 0, controls: 1, rel: 0, playsinline: 1, cc_load_policy: 0 }},
        events: {{
            onReady: () => {{
                document.getElementById("overlay").textContent = "按播放開始";
                setInterval(updateSubtitle, 180);
            }}
        }}
    }});
}}
</script>
</body>
</html>
"""


st.markdown('<div class="app-title">🎬 YouTube 同步中文字幕</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="app-subtitle">極保守模式：優先字幕 API；請求之間主動等待；遇到 429/403 立即停止並冷卻 20 分鐘。</div>',
    unsafe_allow_html=True,
)

url = st.text_input(
    "YouTube 網址",
    placeholder="例如：https://music.youtube.com/watch?v=xxxxxxxxxxx",
)

with st.expander("辨識設定", expanded=False):
    allow_speech_recognition = st.checkbox(
        "找不到字幕時，允許使用語音辨識（極保守模式預設關閉）",
        value=False,
        help="開啟後只有在沒有 429/403、且前兩種字幕方式都失敗時，才會等待後只嘗試一次音訊下載。",
    )
    cookie_upload = st.file_uploader(
        "cookies.txt（選填，僅登入／年齡限制影片需要）",
        type=["txt"],
        help="檔案只在這次處理期間暫存，請勿上傳到公開 GitHub。",
    )
    model_label = st.selectbox(
        "Whisper 模型",
        ["tiny（最快、準確度較低）", "base（建議）", "small（較準確、較慢）"],
        index=1,
    )
    model_size = {
        "tiny（最快、準確度較低）": "tiny",
        "base（建議）": "base",
        "small（較準確、較慢）": "small",
    }[model_label]

col1, col2 = st.columns([1, 4])
with col1:
    load_button = st.button("讀取／產生中文字幕", type="primary", use_container_width=True)
with col2:
    st.caption("極保守順序：字幕 API → 等待 → 低頻率 yt-dlp →（手動允許才會）Whisper；429/403 立即停。")

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
                cookie_upload.getvalue() if cookie_upload is not None else None,
            )
            if not subtitles:
                raise RuntimeError("字幕內容是空的，無法顯示。")

            st.session_state["video_id"] = video_id
            st.session_state["subtitles"] = subtitles
            st.session_state["source_note"] = source_note
            status.update(label="中文字幕已完成", state="complete", expanded=False)
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

    st.iframe(
        build_player(st.session_state["video_id"], subtitles),
        height=820,
    )

    with st.expander("限制與準確度說明"):
        st.write(
            "字幕時間會依照 YouTube 字幕或 Whisper 語音片段同步。歌曲、背景音樂過大、多人同時說話、口音與專有名詞，都可能造成辨識或翻譯誤差。"
        )
        st.write(
            "語音辨識模式會暫時取得音訊，完成後自動刪除；影片仍由 YouTube 官方播放器播放。"
        )
        st.warning(
            "Streamlit 免費雲端的共享 IP 仍可能遭 YouTube 封鎖。可在 Streamlit Secrets 設定 YOUTUBE_PROXY（建議住宅輪替代理），成功率會比只放 cookies.txt 高。"
        )
