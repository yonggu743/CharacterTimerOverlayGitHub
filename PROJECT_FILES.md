# GitHub 上传文件清单

建议上传这些文件：

- `README.md`
- `PROJECT_FILES.md`
- `requirements.txt`
- `.gitignore`
- `scripts/build_overlay.ps1`
- `tools/character_timer_overlay.py`
- `tools/screen_reader.py`
- `tools/character_timers_db.json`
- `release/CharacterTimerOverlay/`：可运行的打包版本。如果仓库不想放二进制文件，可以改为上传到 GitHub Releases。

可以保留但不是主程序必需：

- `tools/character_timer_manager.py`：旧版独立数据库管理器。现在数据库页面已经整合进 `CharacterTimerOverlay.exe`。

不建议上传到源码仓库：

- `build/`：PyInstaller 中间构建目录。
- `dist/`：本地打包输出目录；当前用于上传的可运行版本已经复制到 `release/CharacterTimerOverlay/`。
- `*.log`、截图、临时备份文件。
- `planner/`：另一个代码恢复任务的文件，和本工具无关。
- 早期实验脚本：`tools/keyboard_sim_demo.py`、`tools/text_trigger_f.py`、`tools/text_trigger_gui.py`、`tools/overlay_countdown.py`、`tools/virtual_hid_keyboard/`。
