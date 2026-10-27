---
name: ui-design
description: 界面设计与动效的统一入口。处理网页、应用界面、组件的构建、改版、体验审查和交互优化，按任务、平台及已有技术栈选择对应专长 Skill。需要协调设计、审查、动效等多个方向时优先使用；后端逻辑、普通 Swift 并发、图表数据分析与文档排版不属于本入口。
---

# UI 设计总入口

把用户需求交给合适的专长，并协调各专长的边界。入口负责选择、组合与交接；实际设计和实现遵循选定专长的完整规范。

## 选择与交接

1. 从已有上下文和项目文件确认：目标页面或组件、产品界面或品牌页面、构建/修改/只读审查、Web/React Native/SwiftUI，以及已使用的设计系统和动效技术。已有信息足够就继续；影响目标或改动范围的歧义才询问。
2. 按下表选择最贴近目标的主专长。一般先加载一个；出现具体的技术或交互问题时再加载相关辅助专长。使用一个主视觉规范，避免把几套互相冲突的风格规则同时套到同一界面。
3. 在支持 Skill 调用的环境中调用所选专长；否则读取对应 `SKILL.md`。表中的路径相对此文件，先解析当前技能的实际目录（含 Junction 目标），再定位相邻技能。子技能中的脚本和参考资料以该子技能实际目录为基准，命令的工作目录仍是用户项目。不要假定存在 `.claude/skills` 或 `.agents/skills`。
4. 需要了解专长的能力、依赖、适用边界或已知问题时，查阅[专长目录](references/specialists.md)对应条目，然后读取被选中专长的完整入口及它要求的参考。目录介绍不能替代专长规范。若文件不存在，明确说明缺少哪个专长，使用已安装的合适替代或询问用户，避免自动下载安装。
5. 用一句话说明选择，例如“这次是已有后台的间距调整，采用 Impeccable 的 layout 流程并保留现有组件”。交接用户目标、范围、技术栈、已知品牌约束和验证要求，再执行任务。检查、比较、打分等只读请求保持只读。

## 主任务分派

| 实际需求 | 先读取 | 使用边界 |
|---|---|---|
| 后台、工具、表单、设置等产品 UI；修复布局、文案、响应式或状态 | [impeccable](../impeccable/SKILL.md) | 选择对应子命令及 product register；已有设计系统优先 |
| UI 体验或技术审查、精修、设计系统提取 | [impeccable](../impeccable/SKILL.md) | 体验评议用 critique，技术检查用 audit，改进按请求用 polish/layout/clarify 等；保留各流程的只读边界 |
| 新品牌网页、作品集或强调视觉概念的页面 | [frontend-design](../frontend-design/SKILL.md) | 建立一个视觉方向；已有品牌页面可选择 Impeccable brand register 处理 |
| 用户明确指定 Taste、design-taste-frontend 或手动调用该专长 | [design-taste-frontend](../design-taste-frontend/SKILL.md) | 保留仅显式调用政策；普通“做个好看的页面”不足以启用 |
| 广告、活动、注册等单目标落地页的转化结构 | [landing-page-generator](../landing-page-generator/SKILL.md) | 负责目标、信息结构与 CTA；需要实现视觉时再选一个视觉主专长；普通多用途首页不自动走此流程 |
| 组件质感、交互细节与设计工程原则的讨论 | [emil-design-eng](../emil-design-eng/SKILL.md) | 提供设计判断；不是额外一套必须覆盖现有品牌的视觉主题 |
| Web 动效的目的、节奏、进退场、可中断交互和实现 | [animate](../animate/SKILL.md) | 简单 hover/fade 优先复用现有 CSS；保持用户选定或项目已有动效库 |
| Expo / React Native 手势、动画、触感 | [animate-expo](../animate-expo/SKILL.md) | 使用原生平台路径；不分派到 DOM/GSAP 模块 |
| 用户要求 Apple 风格、弹簧或物理交互语言 | [apple-design](../apple-design/SKILL.md) | 可作为指定的交互规范；需要 Web 动效实现时再结合 animate |
| “这个动效叫什么”、从描述查术语 | [animation-vocabulary](../animation-vocabulary/SKILL.md) | 回答术语与区别；不扩大为实现任务 |
| 用户明确点名 review-animations 审阅组件或动效 diff | [review-animations](../review-animations/SKILL.md) | 该专长禁止隐式调用；普通检查按体验或技术目标选 Impeccable critique 或 audit，审阅不代表授权修改 |
| 整个代码库的动效审计与改进计划 | [improve-animations](../improve-animations/SKILL.md) | 只读调研，输出计划；用户要求实施时再转实现专长 |
| 寻找尚未添加但有必要的动效 | [find-animation-opportunities](../find-animation-opportunities/SKILL.md) | 只读提出机会；允许得出无需增加动效的结论 |
| Sonner toast 的用法、样式或故障 | [ask-sonner](../ask-sonner/SKILL.md) | 先确认涉及 Sonner；普通提示文案问题可走 Impeccable clarify |
| 用户明确要求选库、比较库或调用 pick-ui-library | [pick-ui-library](../pick-ui-library/SKILL.md) | 保留显式调用限制；普通界面任务不顺带安装依赖 |
| 用户明确要求多套界面方案、方案选择器或调用 prototype | [prototype](../prototype/SKILL.md) | 多方案探索；普通“做原型”不擅自扩展为多版本比较 |
| SwiftUI 原生界面的代码或交互实现 | [write-swift](../write-swift/SKILL.md) | 平台技术辅助；纯 Swift 并发/数据逻辑交回该专长直接处理，无需经过 UI 入口 |

## GSAP 专长分派

用户已经选择 GSAP、项目已有 GSAP，或明确的复杂编排需求经判断适合 GSAP 时，再进入本表。仅出现 React、动画、滚动或卡顿这些词不构成更换现有库的理由。先读取项目依赖与实际使用版本；涉及安装方式、插件授权或版本专有 API 时核对官方现行资料。

| GSAP 中的具体问题 | 按需读取 |
|---|---|
| tween/easing、响应式、减少动态效果、基础 API | [gsap-core](../gsap-core/SKILL.md) |
| React / Next.js 的作用域、useGSAP、生命周期和清理 | [gsap-react](../gsap-react/SKILL.md) |
| Vue / Nuxt / Svelte 等非 React 框架接入 | [gsap-frameworks](../gsap-frameworks/SKILL.md) |
| 多步动画、标签、嵌套与播放控制 | [gsap-timeline](../gsap-timeline/SKILL.md) |
| 滚动绑定、pin、scrub、刷新与容器滚动 | [gsap-scrolltrigger](../gsap-scrolltrigger/SKILL.md) |
| Flip、Draggable、SplitText 等具体插件 | [gsap-plugins](../gsap-plugins/SKILL.md)的对应章节 |
| 已定位到 GSAP 的性能瓶颈 | [gsap-performance](../gsap-performance/SKILL.md) |
| 已采用 GSAP 的数值映射、集合或工具函数 | [gsap-utils](../gsap-utils/SKILL.md)的对应函数 |

例如 React 的滚动联动可选 gsap-react 与 gsap-scrolltrigger；需要基础 tween 或 reduced-motion 细节时补 core；需要多步编排时再补 timeline。不要固定加载整套 GSAP 专长。实现滚动或框架接入前先读目录中的已知示例问题，检查单位、生命周期与清理作用域。

## 组合时的约束

- 用户的明确目标、现有品牌、框架、依赖和改动范围优先于子技能的默认偏好。风格意见相冲突时，沿用已选的主视觉或平台规范；技术正确性、可访问性及数据保护要求继续保留。不要凭一个子技能的推荐擅自更换框架、字体或动画库。
- 单次任务选一个主视觉负责人。落地页结构、平台 API、动效或 Sonner 可以辅助该负责人；不同时叠加 frontend-design、Taste 和 Impeccable 的全部审美规则。明确 Apple 弹簧需求时采用相应交互语言，而不是被另一模块的默认“禁止回弹”偏好否决。
- 子技能的显式调用限制继续有效，包括 Taste、pick-ui-library、prototype 和 review-animations。使用本总入口不会自动开放这些专长；需要用户明确点名该专长，或明确授权按该专长流程执行。一般“好看”“原型”“检查一下”不足以启用；普通检查按体验或技术目标选 Impeccable critique 或 audit，普通单方案原型仍按产品或品牌任务处理。
- 代码审查、UX 审计和寻找动效机会按原专长保持只读。若用户同时授权修复，可在报告后使用实现专长处理批准的范围。子技能的研究建议不自动授权创建其他任务、发消息、发布或改全局配置。
- Impeccable 的 pin、hooks、更新等管理操作按用户明确意图执行，不作为普通设计前置步骤。离线或禁止后台更新检查的任务可使用其已有的 `IMPECCABLE_NO_UPDATE_CHECK` 选项，保留正常上下文读取。用户明确“不写文件/不联网”时，也避开更新缓存、持久化快照和报告等写入路径；必要时直接读取源码完成检查，说明未执行哪些工具流程。
- 控制脚本副作用：先读实际要执行的脚本与依赖；缺资源就说明并降级。不要执行缺失的 Taste scroll-scene 分支；不要把落地页参考中的未证实数字写成真实客户数据。
- 交付验证与任务相称：审查给出证据，界面实现检查主要交互、响应式和键盘操作；动效检查可中断、卸载清理与 reduced-motion。缺少浏览器或运行环境时明确标注未验证，不以静态评分替代运行结论。

## 维护关系

[专长目录](references/specialists.md)记录本地已安装的 24 个专长及已知限制。原 `emil-frontend` 作为兼容名称转入本入口；本入口直接选择真实专长，不再转回旧入口，避免循环。专长独立保存和更新；新增或更新专长后应复核目录、触发政策、资源存在性和路由用例。
