# 整合入口

这些文件夹保存本项目维护的总入口，用于选择实际安装的专长。总入口不自动启动独立 Agent，也不包含全部第三方脚本和资源。

| 入口 | 用途 | 专长安排 |
|---|---|---|
| [ui-design](ui-design/SKILL.md) | UI、体验审查与动效 | 设计、平台和动效按任务加载 |
| [document-workflow](document-workflow/SKILL.md) | PDF、OCR、演示稿、网页与图解 | 按文件类型与输出选择制作链路 |
| [development-workflow](development-workflow/SKILL.md) | 规划、规格实施、调试与测试 | 已内置最小调试流程，OpenSpec 按阶段选择 |
| [writing-workflow](writing-workflow/SKILL.md) | 中文或中英文表达润色 | 选择一个主专长，保留事实与作者立场 |
| [f-d-u-d](f-d-u-d/SKILL.md) | 查找、创建、检查和整理技能 | 本地重写的技能管理入口，保留旧名称 |
| [skill-curator](skill-curator/SKILL.md) | 评价与专长整合 | 区分静态判断、路由推演与实际任务验证 |
| [emil-frontend](emil-frontend/SKILL.md) | 旧名称兼容 | 单向交给 ui-design |

## 安装与依赖

将所选文件夹导入自己的技能库，再通过管理工具接入各 Agent。总入口和专长应能按目录中标注的相邻路径找到；同名内容不同时先预览差异，不覆盖现有改造。

专长目录内的相对链接面向安装后的技能库。仓库未附带的专长，在 GitHub 页面中可能没有对应目标；它们是外部运行依赖，不是遗漏的打包文件。当前四个新增/重写入口的依赖清单见 [runtime-dependencies.json](runtime-dependencies.json)。已有 UI 专长关系见其 [目录](ui-design/references/specialists.md)。

外部技能的来源、许可证、资源完整性与实际兼容性应分别检查；仓库的 MIT 许可证不改变外部技能的许可。缺少专长时报告缺口，不把目录存在当作可以运行，也不自动安装全部依赖。

## OCR 的本地修复

文档入口要求本地 OCR 失败时停止。本轮本机安装的 OCR 已修复，外部原版包可能仍有自动切云端行为。使用相关路径前检查初始化逻辑；必要时按 [补丁说明](../patches/README.md) 应用局部修复，或停止该路径，不凭总入口文字推定外部脚本已经安全。

这些入口不随应用启动自动安装，也不自动删除现有独立技能。整理过程与验证范围见 [整理记录](../docs/skill-curation-and-cleanup.md)。
