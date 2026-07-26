# YouTube 同步中文字幕 App

## 功能
- 支援 YouTube 與 YouTube Music 網址
- 上方播放原始 YouTube 影片
- 下方顯示帶時間碼的中文字幕稿
- 字幕跟隨播放時間自動反白、捲動
- 點擊字幕可跳到對應秒數
- 影片畫面可顯示同步中文字幕

## Windows 安裝方式

1. 安裝 Python 3.11 或 3.12。
2. 解壓縮本專案。
3. 在資料夾空白處按住 Shift 並按滑鼠右鍵，開啟 PowerShell。
4. 執行：

```powershell
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

瀏覽器會自動開啟 App。

## 注意
- 影片必須有可讀取字幕，或 YouTube 提供可翻譯字幕。
- 私人影片、年齡限制、地區限制、字幕關閉或部分音樂內容可能無法讀取。
- 翻譯精準度取決於原始字幕與 YouTube 翻譯品質。
- 本程式不下載影片，影片透過 YouTube 官方播放器播放。
