# 文档专长目录

路径相对本文件。读取对应入口后再找其具体资源；不存在的分支不能凭名称猜命令。

| 专长 | 职责与输入 | 资源与输出 | 限制与关系 |
|---|---|---|---|
| [pdf](../../pdf/SKILL.md) | PDF 读写、页操作、提取、表单 | `REFERENCE.md`、`FORMS.md` 与自身 scripts；交付 PDF 或提取数据 | 表单先读 FORMS；与 OCR 互补，不能以识别替代数字文本提取 |
| [pdf-ocr-skill](../../pdf-ocr-skill/SKILL.md) | 扫描 PDF、图片；明确引擎、本地/云端约束 | `scripts/pdf_ocr_processor.py`、按需 usage；交付文字及实际引擎信息 | 缺依赖可能触发安装流程，先检查；本地失败停止，云端需显式选择 |
| [pptx](../../pptx/SKILL.md) | 既有 PPTX/POTX、模板修订、新建 | 自身脚本、OOXML/PPTXGenJS 流程；可编辑 PPTX | 现有本地偏好为新建先看可见风格样例；本地专长带独立许可证，不复制到总入口 |
| [ppt-master](../../ppt-master/SKILL.md) | 材料驱动的 SVG 演示稿制作 | roles、workflows、references、导出工具；SVG/PPTX | 这是完整制作链路，不能只抄一个导出命令；演讲音频与云端语音另按实际授权 |
| [defuddle](../../defuddle/SKILL.md) | 普通网页 URL | CLI 输出 Markdown/JSON | 不用来重处理原始 Markdown；未安装 CLI 时不要擅自全局安装 |
| [archify](../../archify/SKILL.md) | 架构、流程、关系与状态的语义资料 | `bin/archify.mjs`、schema、示例；独立交互 HTML | 非数值统计图专长；版本检查可能联网，严格离线时不要执行该分支 |
| [json-canvas](../../json-canvas/SKILL.md) | `.canvas` 节点、边与分组 | Canvas JSON 结构；可编辑画布 | 与交互 HTML 输出互补，不为了统一而改格式 |

这些技能独立保存和更新，总入口只维护选择与组合。文件存在性检查不证明 OCR、Office 渲染或第三方 CLI 在当前环境可运行。
