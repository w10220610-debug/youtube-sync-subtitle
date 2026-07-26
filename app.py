
import html
import json
import re
from urllib.parse import parse_qs, urlparse

import streamlit as st
import streamlit.components.v1 as components
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

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


def transcript_items_to_dicts(fetched) -> list[dict]:
    result = []
    for item in fetched:
        text = html.unescape(item.text).replace("\n", " ").strip()
        if not text:
            continue

        start = float(item.start)
        duration = max(float(item.duration), 0.15)
        result.append(
            {
                "start": round(start, 3),
                "duration": round(duration, 3),
                "end": round(start + duration, 3),
                "text": text,
            }
        )
    return result


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
    for code in ("zh-Hant", "zh-TW", "zh-HK", "zh", "zh-Hans", "zh-CN"):
        if code in available:
            return code
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def load_subtitles(video_id: str) -> tuple[list[dict], str]:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)

    chinese_codes = ["zh-Hant", "zh-TW", "zh-HK", "zh", "zh-Hans", "zh-CN"]
    existing_chinese = find_existing_transcript(transcript_list, chinese_codes)

    if existing_chinese:
        kind = "自動產生" if existing_chinese.is_generated else "人工建立"
        return (
            transcript_items_to_dicts(existing_chinese.fetch()),
            f"使用影片現有中文字幕（{kind}｜{existing_chinese.language}）",
        )

    source = find_existing_transcript(
        transcript_list,
        ["en", "en-US", "en-GB", "en-CA", "en-AU"],
    )

    if source is None:
        candidates = list(transcript_list)
        source = next((x for x in candidates if x.is_translatable), None)

    if source is None:
        raise NoTranscriptFound(
            video_id,
            ["zh-Hant", "zh-TW", "zh", "en"],
            transcript_list,
        )

    if not source.is_translatable:
        raise RuntimeError(
            f"找到「{source.language}」字幕，但 YouTube 沒有提供中文翻譯。"
        )

    target_code = pick_translation_code(source)
    if not target_code:
        raise RuntimeError("YouTube 目前沒有提供這部影片的中文翻譯選項。")

    translated = source.translate(target_code)
    kind = "自動辨識" if source.is_generated else "人工字幕"
    return (
        transcript_items_to_dicts(translated.fetch()),
        f"由 {source.language} {kind}翻譯為中文",
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
body {{
    margin: 0;
    background: #0e1117;
    color: #f7f8fa;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft JhengHei", sans-serif;
}}
.grid {{
    display: grid;
    grid-template-columns: minmax(0, 1.65fr) minmax(330px, .85fr);
    gap: 14px;
    height: 790px;
}}
.panel {{
    background: #151a22;
    border: 1px solid #303846;
    border-radius: 14px;
    overflow: hidden;
}}
.video-wrap {{
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: black;
}}
#player {{ width: 100%; height: 100%; }}
.caption-overlay {{
    position: absolute;
    left: 5%;
    right: 5%;
    bottom: 5%;
    z-index: 10;
    text-align: center;
    pointer-events: none;
}}
.caption-overlay span {{
    display: inline-block;
    max-width: 100%;
    padding: 8px 13px;
    border-radius: 8px;
    background: rgba(0,0,0,.78);
    font-size: clamp(18px, 2.1vw, 30px);
    font-weight: 750;
    line-height: 1.45;
    text-shadow: 0 2px 3px #000;
}}
.controls {{
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 12px;
    border-top: 1px solid #303846;
}}
button {{
    border: 1px solid #394353;
    background: #202733;
    color: white;
    padding: 8px 12px;
    border-radius: 9px;
    cursor: pointer;
}}
button:hover {{ background: #2b3442; }}
#time {{ color: #aab2bf; font-variant-numeric: tabular-nums; }}
.transcript-panel {{
    display: flex;
    flex-direction: column;
    min-height: 0;
}}
.header {{
    padding: 13px 15px;
    border-bottom: 1px solid #303846;
    font-weight: 800;
}}
#transcript {{
    overflow-y: auto;
    padding: 9px;
    scroll-behavior: smooth;
}}
.line {{
    display: grid;
    grid-template-columns: 58px 1fr;
    gap: 8px;
    padding: 10px 9px;
    margin-bottom: 5px;
    border-radius: 9px;
    cursor: pointer;
    border: 1px solid transparent;
    line-height: 1.55;
}}
.line:hover {{ background: #202733; }}
.line.active {{
    background: #273851;
    border-color: #4e7ebc;
}}
.timestamp {{
    color: #82aef0;
    font-size: 13px;
    padding-top: 3px;
    font-variant-numeric: tabular-nums;
}}
.text {{ font-size: 16px; }}
@media (max-width: 850px) {{
    .grid {{
        display: block;
        height: auto;
    }}
    .transcript-panel {{
        height: 430px;
        margin-top: 14px;
    }}
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
        row.onclick = () => {{
            if (player && player.seekTo) {{
                player.seekTo(item.start, true);
                player.playVideo();
            }}
        }};

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
    let low = 0;
    let high = subtitles.length - 1;
    let answer = -1;

    while (low <= high) {{
        const mid = Math.floor((low + high) / 2);
        if (subtitles[mid].start <= time) {{
            answer = mid;
            low = mid + 1;
        }} else {{
            high = mid - 1;
        }}
    }}

    if (answer < 0) return -1;

    const item = subtitles[answer];
    const nextStart = answer + 1 < subtitles.length
        ? subtitles[answer + 1].start
        : item.end + 2;

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
            activeLine.scrollIntoView({{ behavior: "smooth", block: "center" }});
        }}
        overlay.textContent = subtitles[index].text;
    }} else {{
        overlay.textContent = "";
    }}
}}

function jumpRelative(seconds) {{
    if (!player || !player.getCurrentTime) return;
    player.seekTo(Math.max(0, player.getCurrentTime() + seconds), true);
}}

function toggleOverlay() {{
    overlayVisible = !overlayVisible;
    document.querySelector(".caption-overlay").style.display =
        overlayVisible ? "block" : "none";
}}

renderTranscript();

const tag = document.createElement("script");
tag.src = "https://www.youtube.com/iframe_api";
document.head.appendChild(tag);

function onYouTubeIframeAPIReady() {{
    player = new YT.Player("player", {{
        videoId: "{video_id}",
        playerVars: {{
            autoplay: 0,
            controls: 1,
            rel: 0,
            playsinline: 1,
            cc_load_policy: 0
        }},
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
    '<div class="app-subtitle">貼上 YouTube 或 YouTube Music 網址，影片與中文字幕稿會同步播放。</div>',
    unsafe_allow_html=True,
)

url = st.text_input(
    "YouTube 網址",
    placeholder="例如：https://music.youtube.com/watch?v=xxxxxxxxxxx",
)

col1, col2 = st.columns([1, 4])
with col1:
    load_button = st.button("讀取影片字幕", type="primary", use_container_width=True)
with col2:
    st.caption("只有影片本身存在字幕，或 YouTube 提供可翻譯字幕時，這個版本才能讀取。")

if load_button:
    video_id = extract_video_id(url)

    if not video_id:
        st.error("網址格式不正確，請貼上完整 YouTube／YouTube Music 網址。")
    else:
        with st.spinner("正在讀取並整理中文字幕……"):
            try:
                subtitles, source_note = load_subtitles(video_id)

                if not subtitles:
                    st.error("字幕內容是空的，無法顯示。")
                else:
                    st.session_state["video_id"] = video_id
                    st.session_state["subtitles"] = subtitles
                    st.session_state["source_note"] = source_note

            except TranscriptsDisabled:
                st.error("這部影片已關閉字幕，因此目前無法讀取。")
            except VideoUnavailable:
                st.error("影片不存在、私人影片，或目前無法播放。")
            except NoTranscriptFound:
                st.error("找不到可用的中文字幕、英文字幕或可翻譯字幕。")
            except CouldNotRetrieveTranscript as exc:
                st.error(f"暫時無法從 YouTube 取得字幕：{exc}")
            except Exception as exc:
                st.error(f"讀取失敗：{exc}")

if "video_id" in st.session_state and "subtitles" in st.session_state:
    st.markdown(
        f'<div class="status-box">✅ {st.session_state["source_note"]}｜共 '
        f'{len(st.session_state["subtitles"])} 段字幕</div>',
        unsafe_allow_html=True,
    )

    components.html(
        build_player(
            st.session_state["video_id"],
            st.session_state["subtitles"],
        ),
        height=820,
        scrolling=False,
    )

    with st.expander("查看限制與準確度說明"):
        st.write(
            "字幕時間會依照 YouTube 提供的時間碼同步。翻譯準確度則取決於原始字幕品質；"
            "歌曲、背景音樂過大、多人同時說話、口音或專有名詞，都可能造成辨識或翻譯誤差。"
        )
        st.write(
            "這個版本不下載影片，也不重新上傳影片；影片仍由 YouTube 官方播放器播放。"
        )
