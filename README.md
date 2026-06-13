# UFC Keypad v1.0

**F/A-18C Hornet UFC 触控面板** — 基于 DCS-BIOS 协议的第 2 屏外设

---

## 屏幕截图

启动后分为两个窗口：

- **设置窗口**（主屏）：选择显示器、查看按键日志、调整参数
- **UFC 面板**（目标屏）：1024×600 全屏无边框，直接触控操作

面板风味还原 F/A-18 superhornet 座舱 UFC 外观：绿色点阵字体、5 行 × 4 列 + 长条 scratchpad、COMM1/COMM2 显示、OSB 标记及 EM CON 开关。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| **DCS-BIOS 实时通信** | UDP 协议读取 UFC 显示数据 + 发送按键指令，零延迟 |
| **触控原生输入** | 触摸屏 Native Touch 模式，按压/释放完全对应 DCS 按钮行为 |
| **Scratchpad 动态显示** | 三字段独立渲染（str1 / str2 / number），右对齐，实时更新 |
| **COMM 频道调节** | COMM1/COMM2 左右旋转 + Pull 拉起，模拟旋钮操作 |
| **OSB 按键** | 5 个 Option Select 按钮，从上到下对应座舱 OSB1-5 |
| **EM CON 支持** | 无线电静默按钮直接映射 |
| **亮度同步** | 读取 DCS 座舱 `UFC_BRT` 旋钮，自动调节面板文字/边框亮度 |
| **DCS 断连保护** | 游戏退出或信号丢失后 3 秒自动清空显示、亮度降至最低 |
| **多屏无损** | 可指定任意副屏全屏显示，不抢主屏焦点 |
| **图标** | 1024×1024 高清应用图标 |

---

## 安装与运行

### 可执行文件（推荐）

下载 `UFC_Keypad_v1.0.exe`，双击运行即可。首次启动会在同目录生成配置文件。

> 需要 **DCS-BIOS Skunkworks** 已在 DCS World 中安装并运行。

### 源码运行

```bash
# 依赖
pip install PyQt6

# 运行
python ufc_keypad.py

# 打包
pip install pyinstaller
pyinstaller ufc_keypad.spec
```

---

## 使用说明

1. 启动 DCS World，进入 F/A-18C 座舱（确保 DCS-BIOS 已加载）
2. 运行 `UFC_Keypad_v1.0.exe`
3. 在设置窗口选择目标显示器 → 点击 **"应用到显示器"**
4. 触控或鼠标点击 UFC 面板按钮，操作会实时发送到 DCS

### 夜间 / 低亮度环境

DCS 座舱内调节 UFC 亮度旋钮（BRT）→ 面板文字和边框亮度自动跟随。

### DCS 重新加载

退出任务重修时，面板自动清空显示，进入新任务后立即恢复。

---

## 致谢

- **字体来源**：[BlueFinBima/Helios-DCS-Fonts](https://github.com/BlueFinBima/Helios-DCS-Fonts) — F/A-18C Hornet UFC 真实点阵字体
- **DCS-BIOS**：[DCSFlightpanels/dcs-bios](https://github.com/DCSFlightpanels/dcs-bios) — DCS World 数据导出与控制协议
- **DCS World**：[Eagle Dynamics](https://www.digitalcombatsimulator.com/) — 无与伦比的飞行模拟平台

---

## 技术栈

- **Python 3.13+** / **PyQt6** — GUI 框架
- **DCS-BIOS Skunkworks UDP** — 数据协议（端口 5010 接收 / 7778 发送）
- **ctypes + Win32 API** — 触摸隔离、窗口激活拦截
- **PyInstaller** — 打包为独立 exe

---

## License

MIT License © 2026
