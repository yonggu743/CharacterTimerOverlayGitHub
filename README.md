# Character Timer Overlay

一个 Windows 桌面悬浮计时工具，用于根据屏幕中识别到的角色名、当前角色序号和键盘按键，显示角色技能 CD 与 Buff 剩余时间。

项目当前主程序是 `CharacterTimerOverlay.exe`，源码入口为 `tools/character_timer_overlay.py`。数据库管理界面已经整合进主程序，不再需要单独打开旧版 `CharacterTimerManager.exe`。

## 功能

- 实时读取指定屏幕区域，并用 OCR 识别队伍角色名。
- 通过 `1`、`2`、`3`、`4` 切换当前角色。
- 监听技能按键，触发当前角色的技能 CD 倒计时。
- 监听 Buff 按键，触发 Buff 剩余时间倒计时。
- 技能 CD 显示在角色名左侧，并跟随角色所在位置。
- 技能 CD 使用半透明 HUD 胶囊显示，包含剩余秒数和进度条。
- Buff 统一显示在一个半透明 HUD 面板中，按剩余时间从少到多排序。
- 主界面提供启动/停止、OCR 开关、键盘监听开关、悬浮显示开关。
- 内置角色数据库页面，可增删改查角色、别名、技能按键、技能 CD、Buff 按键和 Buff 持续时间。
- 支持鼠标框选 OCR 识别区域，不需要手动填写坐标。
- 角色数据库支持按角色名或别名实时搜索。

本工具只读取屏幕和键盘事件，并显示悬浮窗口；不包含自动按键、宏或输入模拟功能。

## 运行环境

- Windows 10/11
- Python 3.12 测试通过
- 建议以管理员身份运行，这样全局键盘监听更稳定

## 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python tools\character_timer_overlay.py
```

如果想启动后自动开始识别和监听：

```powershell
python tools\character_timer_overlay.py --auto-start
```

## 打包 exe

安装依赖后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_overlay.ps1
```

打包结果在：

```text
dist\CharacterTimerOverlay\CharacterTimerOverlay.exe
```

发布给别人使用时，请把整个 `dist\CharacterTimerOverlay\` 文件夹一起压缩发布，不要只复制单个 exe。`character_timers_db.json` 需要和 exe 放在同一个目录。

## 基本使用

1. 打开 `CharacterTimerOverlay.exe`。
2. 在 `运行控制` 页面点击 `框选识别区域`，用鼠标拖出角色名所在区域。
3. 点击 `启动`，开始 OCR、键盘监听和悬浮显示。
4. 用键盘 `1`、`2`、`3`、`4` 切换当前角色。
5. 按下角色配置里的技能按键后，对应角色开始技能 CD 倒计时。
6. 按下角色配置里的 Buff 按键后，Buff 面板显示剩余时间。
7. 点击 `停止` 可关闭 OCR、键盘监听并隐藏悬浮倒计时。

## 角色数据库

配置文件是：

```text
tools\character_timers_db.json
```

打包后使用：

```text
dist\CharacterTimerOverlay\character_timers_db.json
```

主界面的 `角色数据库` 页面可以直接编辑：

- `角色名`：显示和匹配用的主名称。
- `别名，逗号分隔`：OCR 可能识别出的其他写法。
- `技能按键`：触发技能 CD 的键，例如 `e`。
- `技能 CD 秒数`：技能倒计时长度。
- `Buff 按键`：触发 Buff 倒计时的键，例如 `q`。可以和技能按键相同。
- `Buff 持续秒数`：Buff 倒计时长度，为 `0` 时不显示 Buff。
- `Buff 显示名称`：Buff 面板里显示的名称。

左侧角色列表上方有搜索框，可按角色名或别名过滤角色。

保存角色后，运行中的识别和计时配置会自动刷新。

## 运行参数说明

- `识别区域`：OCR 读取的屏幕区域。点击 `框选识别区域` 后，用鼠标拖拽选择；坐标会自动写入配置。
- `截屏 FPS`：每秒最多抓取几帧屏幕画面。一般设置为 `2` 或 `3` 即可。
- `OCR 间隔 秒`：正常情况下每隔多久识别一次角色名。
- `匹配阈值`：OCR 文字与角色名/别名的匹配要求，越高越严格。
- `变更确认帧数`：检测到角色名变化后，用几帧做确认。
- `确认间隔 秒`：确认帧之间的等待时间。
- `空识别保留 秒`：短时间识别不到角色名时，保留旧角色位置多久。
- `按键防抖 秒`：同一个按键连续触发的最小间隔。
- `CD 左移像素`：技能 CD 标签相对角色名向左移动的距离。
- `CD 字号`、`CD 透明度`：技能 CD 标签样式。
- `Buff 左移像素`：Buff 面板相对识别区域左边缘向左移动的距离。数值越小，面板越靠右。
- `Buff 上移像素`：Buff 面板相对识别区域上边缘向上移动的距离。
- `Buff 字号`、`Buff 透明度`：Buff 面板样式。

## 常见问题

### 打开后看不到悬浮倒计时

先确认主界面里 `显示悬浮倒计时` 已勾选，并且已经点击 `启动`。只有触发了技能 CD 或 Buff 后，对应倒计时才会显示。

### 按键没有触发

尝试以管理员身份运行程序，并确认 `开启键盘监听` 已勾选。不同前台程序对全局键盘监听的行为可能不同。

### 角色名识别不准

可以在 `角色数据库` 页面给角色增加别名，或调整 `匹配阈值`。如果角色列表位置固定，建议保持较低频率 OCR，例如 `OCR 间隔 秒 = 5`。

### Buff 面板位置不合适

调整 `Buff 左移像素` 和 `Buff 上移像素`。其中 `Buff 左移像素` 越小，面板越靠右。
