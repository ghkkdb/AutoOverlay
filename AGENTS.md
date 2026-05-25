# AutoOverlay 项目规则

- 默认使用简体中文进行说明、注释和文档编写。
- 本项目是 Python/Tkinter 桌面工具，入口为 `main.py`，核心视频处理逻辑在 `overlay_engine.py`。
- 本地运行方式：`python -m pip install -r requirements.txt` 后执行 `python main.py`。
- 依赖记录在 `requirements.txt`，当前使用 `imageio-ffmpeg`、`requests`、`cryptography`、`pyinstaller`。
- 打包默认使用 PyInstaller，执行 `.\build.ps1`，产物在 `dist\AutoOverlay\AutoOverlay.exe`。
- 文本文件读写显式使用 `encoding="utf-8"`；路径处理使用 `pathlib.Path` 或标准路径 API。
- 修改代码后优先使用 `python -m compileall .` 做基础语法验证。
