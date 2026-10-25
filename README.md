# RAW Photo Guide (`raw-photo-guide`)

[![Tests](https://img.shields.io/badge/tests-141%20passed-brightgreen.svg)]()
[![Engine](https://img.shields.io/badge/engine-RawTherapee%205.13-blue.svg)]()
[![Schema](https://img.shields.io/badge/schema-JSON--over--CLI-orange.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)]()

> **面向 AI Agent 的工业级 RAW/JPEG 数字暗房修图向导与执行引擎**  
> 坚持“**用户对审美拥有绝对主权，AI 仅为技术参谋与暗房技师**”的人机协作范式。

---

## 🌟 核心哲学与 SOP 规范 (Philosophy & SOP)

当前多数 AI 图像工具往往走向两个极端：要么通过生成式扩散模型“开盲盒”肆意重绘，破坏照片的真实光学细节与元数据；要么智能体缺乏专业暗房常识，机械调整参数，甚至在“氛围感”话术自嗨中交付死黑或过曝的半成品。

`raw-photo-guide` 经过真实相机样片（如 Nikon Z 系列 NEF）的严苛磨合，确立了以下三大不可动摇的 SOP 门禁准则：

1. **审美的绝对主权归用户 (User Aesthetic Sovereignty)**
   - 色彩风格、明暗取舍、局部去留与情绪基调由用户全权决定。
   - 智能体负责**技术参数换算、物理渲染调用、副作用与代价分析（如提亮可能带来的暗部噪点、压暗可能造成的高光发灰）**。
   - 智能体不擅自替用户做审美结论，而是将选择权以具象的方案对比呈现在用户面前。

2. **感知基线先行 (Perceptual Baseline First)**
   - 明确界定“弱修图”指的是**风格表达的克制**，而非“曝光只能动一点点”。
   - 引擎载入的中性线性模板（如 Rec.2020 Neutral）绝非成品亮度，必须先建立**主体轮廓与关键纹理清晰可辨**的感知基线，再叠加色彩与影调风格。

3. **交付前视觉真实验收（眼睛门禁 / Visual Inspection Gate）**
   - **禁止**仅凭统计数值（如平均亮度 `mean_luma`）或玄学话术自欺欺人交付。
   - 智能体每次渲染后，必须实际读取渲染后的预览图进行视觉核查；针对关键细节，调取 100% 原始尺寸局部切片确认。未通过视觉核验的试片，只能标为“未经视觉验收的诊断试片”。

4. **Keep / Change 持久化追溯**
   - 每一轮修图必须严格拆解：用户满意的保留项（`Keep`）与需要纠正的变更项（`Change`），录入版本化会话，避免在跨轮微调时“修好了前一步却弄丢了后一步”。

---

## 🛠️ 架构与核心功能 (Architecture & Features)

```
                 +-----------------------------------------+
                 |            User / Photographer          |
                 +-------------------+---------------------+
                                     | 自然语言交互 / 审美决断
                                     v
                 +-----------------------------------------+
                 |       AI Coding Agent (Skill SOP)       |
                 | - 需求拆解与风格翻译                     |
                 | - 交付前真机看图自检                     |
                 +-------------------+---------------------+
                                     | JSON-over-CLI (stdin/stdout)
                                     v
                 +-----------------------------------------+
                 |            rt_tool.py (Python)          |
                 | - 参数强类型校验 (JSON Schema)          |
                 | - CAS 乐观锁版本管理 (Session.json)     |
                 | - 自动生成并合成 PP3 配置文件           |
                 +-------------------+---------------------+
                                     | Non-destructive rendering
                                     v
                 +-----------------------------------------+
                 |          RawTherapee 5.13 CLI           |
                 | - 32-bit Float 物理渲染管线             |
                 | - Rec.2020 宽色域工作流                 |
                 | - AMaZE / RCD 去马赛克 & 镜头几何校正   |
                 +-------------------+---------------------+
                                     |
                                     v
                           Full TIFF/JPEG & Previews
```

- **真实工业级引擎驱动**：直连 RawTherapee 5.13 底层命令行接口，保持 32 位浮点计算精度与原生色域，非破坏性输出。
- **纯粹的结构化参数协议 (Structured Adjustments)**：
  - **曝光与影调**：曝光补偿 (`exposure_ev`)、高光压缩/重建 (`highlights`)、阴影补偿、微反差与动态范围。
  - **色彩科学**：白平衡温度/色调双轴微调 (`temperature` / `green`)、色彩饱和度、色调曲线 (Tone Curves)。
  - **去马赛克与降噪**：支持 AMaZE、RCD 以及双重去马赛克（Dual Demosaic），支持亮度与色度独立降噪。
  - **色彩与镜头配置**：原生 Rec.2020 工作流，支持外置 ICC 配置文件解析与哈希校验，支持 Adobe LCP 镜头校正文件。
- **不可变状态机与并发安全 (Session Persistence & CAS)**：
  - 修图会话保存在独立的 `session.json` 中，包含完整的用户意图日志（`confirmed_brief`）、历史修订版本（`revisions`）与调色参数。
  - 渲染必须绑定 `expected_revision`，基于 CAS（Compare-And-Swap）乐观锁机制防止多轮交互中的状态覆盖与冲突。
- **安全的两阶段清理机制 (Two-Phase Retention)**：
  - `prune_history` 机制支持预检（Preflight）生成即将清理的孤立试片清册，获得授权后方可执行清理，永久保护最新成片与审计链路。

---

## 📂 目录结构 (Directory Layout)

```text
raw-photo-guide/
├── SKILL.md                 # 核心 Agent SOP 与技能规范说明书 (v0.6.7)
├── plugin.json              # 插件元数据与能力声明
├── README.md                # 项目文档
├── .gitignore               # 排除缓存与临时文件
├── agents/
│   └── openai.yaml          # OpenAI / 兼容 Agent 平台的接口声明
├── assets/
│   ├── neutral-rec2020-rt513.pp3       # 基础中性 Rec.2020 渲染预设模板
│   └── lens_profiles/
│       └── viltrox-af-50f2-z.lcp       # 唯卓仕 50mm f/2.0 Z 实验性镜头校正文件
├── references/              # 按需阅读的技术与工作流文档
│   ├── dialogue.md          # 视觉对齐、风格拆解与 Keep/Change 提问规范
│   ├── export-and-retention.md # 色域 ICC、元数据、线程及试片精简
│   ├── lens-profiles.md     # 镜头校正文件来源、绑定与免责要求
│   ├── raw-controls.md      # RAW 控制项、去马赛克与继承规则
│   ├── raw-workflow.md      # 感知基线、交付门禁与实操工作流
│   ├── tool-api.md          # 完整 JSON-over-CLI 接口规格
│   └── tool-schema.json     # 机器可校验的 JSON Schema
└── scripts/
    ├── rt_tool.py           # 核心控制器：JSON-over-CLI、Session 管理与 RT 封装
    ├── smoke_raw.py         # 真实 RAW 文件烟雾验证与诊断脚本
    └── test_rt_tool.py      # 141 项全覆盖自动化回归测试套件
```

---

## 🚀 环境依赖与安装 (Prerequisites & Installation)

### 1. 系统依赖

- **RawTherapee 5.13**：
  - **macOS**：下载官方 DMG 并安装至 `/Applications/RawTherapee.app`（脚本会自动探测 `/Applications/RawTherapee.app/Contents/MacOS/rawtherapee-cli-bin`）。
  - **Linux**：`sudo apt install rawtherapee` 或官方 AppImage。
  - **Windows**：安装官方发行包，确保 `rawtherapee-cli.exe` 处于系统环境变量 `PATH` 中。
- **ExifTool**（用于读取与保留原始 EXIF 元数据）：
  - macOS: `brew install exiftool`
  - Linux: `sudo apt install libimage-exiftool-perl`
- **Python 3.10+**

### 2. Python 依赖

推荐使用 [`uv`](https://github.com/astral-sh/uv) 直接运行（零手动配置环境），或直接安装以下依赖：

```bash
pip install Pillow numpy jsonschema
```

---

## 💻 快速上手与 CLI 调用示例 (Quick Start)

`rt_tool.py` 采用标准输入输出的 JSON-over-CLI 设计，Agent 或脚本可通过 `--request` 传入 JSON 字符串、文件或 stdin。

### 1. 运行自检 (Doctor)

检查系统引擎、依赖组件与镜头库可用性：

```bash
python3 scripts/rt_tool.py --request '{"action":"doctor"}'
```

输出示例：
```json
{
  "ok": true,
  "result": {
    "engine": {
      "name": "rawtherapee-cli",
      "version": "5.13",
      "available": true
    },
    "exiftool_available": true,
    "structured_adjustments_available": true
  }
}
```

### 2. 初始化修图会话 (Session Init)

```bash
python3 scripts/rt_tool.py --request '{
  "action": "session_init",
  "source_path": "/path/to/DSC_0001.NEF",
  "session_path": "./session_dsc0001/session.json",
  "intent": {
    "purpose": "社交网络分享与作品归档",
    "core_subject": "背光古树中段与青藤",
    "style": "温润微暖、通透清晰",
    "notes": "提升树干与墙体微反差，避免暗部死黑"
  }
}'
```

### 3. 提交调整并渲染试片 (Render)

使用 CAS 机制绑定当前 `expected_revision`（如 `0`），进行结构化微调：

```bash
python3 scripts/rt_tool.py --request '{
  "action": "render",
  "session_path": "./session_dsc0001/session.json",
  "expected_revision": 0,
  "adjustments": {
    "exposure_ev": 0.85,
    "white_balance": {
      "temperature": 5600,
      "green": 1.02
    },
    "tone_curve": {
      "mode": "standard",
      "contrast": 15
    },
    "highlights": {
      "compression": 60,
      "reconstruction": true
    }
  },
  "export": {
    "format": "jpeg",
    "quality": 92,
    "color_space": "sRGB"
  }
}'
```

### 4. 记录用户反馈 (Record Feedback)

当用户审阅试片后提出修改意见，记录 Keep / Change 并推进版本：

```bash
python3 scripts/rt_tool.py --request '{
  "action": "session_record_feedback",
  "session_path": "./session_dsc0001/session.json",
  "raw_feedback": "主体亮度和色彩很舒服，但阴影里的杂草太抢戏，稍微压暗一点",
  "keep": ["树干主体亮度", "5600K温润色温"],
  "change": ["压暗底部暗部杂草与无用细节"]
}'
```

---

## 🧪 自动化测试套件 (Test Suite)

本项目内置严谨的企业级单元测试套件，涵盖版本自洽性、JSON Schema 校验、CAS 并发锁、参数继承与边界检查等。

使用 `uv` 一键执行 141 项测试：

```bash
uv run --script scripts/test_rt_tool.py
```

或使用系统 Python：

```bash
python3 scripts/test_rt_tool.py
```

运行输出：
```text
Ran 141 tests in 16.8s
OK
```

---

## 📄 资产与免责声明 (Assets & Disclaimers)

1. **中性渲染模板 (`assets/neutral-rec2020-rt513.pp3`)**：采用 CC0 / MIT 兼容规范，作为去风格化初始底片。
2. **实验性镜头配置文件 (`assets/lens_profiles/viltrox-af-50f2-z.lcp`)**：
   - 该 Adobe LCP 配置文件为针对唯卓仕（Viltrox）AF 50mm f/2.0 Z 镜头的实验性逆向与测试资源。
   - 官方未提供权威校准曲线，仅供研究与实验使用。正式交付流程中，工具不会在未获用户许可时默认启用未经官方认证的镜头校正。

---

## 🤝 贡献与开源协议 (License)

本项目采用 [MIT License](LICENSE) 开源协议。欢迎提交 Issue、PR 共同完善暗房色彩管线与 Agent SOP！
