# 三个项目的 GPU Broker 接入提示词

先把第 0 节和对应项目区块一起交给项目维护者，并把 `<BROKER_BASE_URL>`、`<PROJECT_TOKEN_SOURCE>` 和本项目路径替换成实际配置

同机进程使用 `http://127.0.0.1:18765`；跨主机后端使用 `https://gpu.aialra.online`，网站前端不能持有项目令牌

令牌只在后端的秘密配置中读取：`projects.minimax`、`projects.live_translate` 或 `projects.manga`；管理员令牌只用于管理控制台，不能交给项目

完整接口及状态解释见 [API.md](API.md)；默认全局准入仍为观察模式，任何一个项目都不能自行开启正式准入

当前 `gpu.aialra.online` 只提供 GPU 准入、调度、状态和监控接口，不转发图片、视频、音频或模型推理数据；三个业务项目仍需分别接入，不能把 Broker 已上线理解为所有调用已经受控

## 0. 三个项目共用的接入指令

```text
请把本项目所有会使用受管 GPU 的路径接入 AIALRA GPU Broker，按照仓库 API.md 和本项目专用区块实现，不要只在网页按钮或单个接口处增加申请逻辑

先审计网页入口、后台队列、定时任务、命令行、服务启动、模型加载与预热、正式推理、批处理、重试、备用引擎、取消和恢复路径，交付“调用入口 → 共用底层模型客户端或启动器 → 实际 GPU 后端”的清单；在最低的共用执行入口加许可检查，使新增上层入口也不能绕过 Broker，正常登录、静态资源及不占用受管 GPU 的请求不需要经过 Broker

网站前端只调用本项目后端；本项目后端通过 BROKER_BASE_URL 调用 Broker 的项目接口，使用本项目专用 Bearer 令牌；跨主机配置 https://gpu.aialra.online，同机配置 http://127.0.0.1:18765，两者是同一个 Broker；令牌只保存在后端秘密配置中，不能进入浏览器、日志、仓库或公开错误消息

在本项目数据库先持久保存业务任务和稳定键，再向 Broker 登记 job、申请对应阶段的 permit 并持久保存返回编号；普通阶段等待许可确认为 ACTIVE 后才能加载模型或调用受管 GPU 后端；实时会话按 API.md 先等待 PREPARING 才预热，准备完成后报告 READY，实际推理仍需 ACTIVE permit；运行期间续期项目、会话和许可心跳，后端确认停止后才能 finish permit 或 close session

Broker 超时、失联、拒绝、冲突或许可不为 ACTIVE 时冻结新的受管 GPU 阶段，保留原任务和后端编号供恢复，不得直接调用模型服务、自动改走显示 GPU 或盲目重发；收到 CANCEL_REQUESTED 后先按后端编号停止执行并确认，再结束许可；最终结果只能发布一次

当前 Broker 是准入与调度层，不是模型数据转发代理；模型请求在获准后仍由项目后端发给原模型服务，不要把媒体或推理请求直接 POST 到 gpu.aialra.online，也不要声称全部模型流量已穿过该域名；若需求是所有模型数据都必须物理经过此域名，先单独设计、实现并验收流式传输、大文件、鉴权、超时、取消与后端路由，现有接口不能代替这一层

交付代码与配置、逐入口审计表、无法按编号查询或取消的后端缺口、显存实测、故障与恢复测试、取消与重复提交测试、前端等待状态、可观测事件及回退步骤；只在观察模式验证，不自行开启全局正式准入
```

## 1. MiniMax H3

```text
请在 MiniMax H3 项目接入 AIALRA GPU Broker，先检查当前代码、未提交改动和实际运行入口，不覆盖现有用户修改

配置 BROKER_BASE_URL=<BROKER_BASE_URL>、BROKER_PROJECT_ID=minimax、BROKER_TOKEN=<PROJECT_TOKEN_SOURCE>，令牌只能由后端读取，不能写入日志、网页、提交记录或错误消息；按仓库 API.md 实现接口语义

审计所有会使用受管 GPU 的视频、图像、音频、相机、放大、模型加载与 ComfyUI 提交路径，交付调用清单，修复每条绕过许可的路径；显示 GPU 不得承担受管模型任务

在本项目数据库持久保存稳定 external_id、idempotency_key、broker_job_id、阶段 request_key、permit_id 和后端任务编号；每个 GPU 阶段先申请 permit，等待 ACTIVE 后才加载模型或向后端提交；等待时上报 WAIT_* 原因并保持项目心跳，运行时续期许可心跳

后端提交前尽可能生成并保存可查询编号；响应丢失时先按编号查询，禁止盲目重发；对项目取消和 Broker 的 CANCEL_REQUESTED 都要按后端编号停止任务，确认停止后才能 finish permit，结果文件只能最终发布一次

Broker 超时、401、403、冲突、遥测失效或非 ACTIVE 状态时停止新的 GPU 阶段，保留原业务任务和后端编号用于恢复，不退回直连 GPU；接入初期仅记录影子观测，正式启用需等待三项目联合验收

交付路径审计表、代码与配置、显存峰值测量、视频和图像固定样例、重复提交和丢响应测试、排队与运行中取消测试、重启/失联恢复、产物去重证据及回退步骤
```

## 2. Live Translate

```text
请在 Live Translate 项目接入 AIALRA GPU Broker，保留现有队列、任务租约、用户修改和音频可靠落盘机制

配置 BROKER_BASE_URL=<BROKER_BASE_URL>、BROKER_PROJECT_ID=live_translate、BROKER_TOKEN=<PROJECT_TOKEN_SOURCE>，令牌只能由后端读取；按仓库 API.md 实现接口语义

先列出模型加载、预热、流式推理、卸载、启动脚本和所有直接 GPU 调用；不要在获得会话准备许可前预热模型，也不要在等待 GPU 时长期占住原业务租约

进程启动时生成新的 instance_id 并持续心跳；申请 session 后等待 PREPARING 才预热，模型和端到端探针通过后发送 ready；每段实际推理还需申请 realtime permit，只有 ACTIVE 才运行，期间续期会话和许可心跳

音频先可靠保存，等待冲突任务结束时如实显示“等待安全边界”；不抢占无法安全停止的视频，不自动迁移到显示 GPU；结束时先确认推理后端停止或模型卸载，再 close session 并上报实测常驻显存

Broker 或项目重启、心跳失败、响应丢失、401/403、非 ACTIVE 状态时冻结新 GPU 阶段，保留音频、业务状态和后端编号供核实；轮询取消请求，先停后端再结束许可，译文只发布一次

交付调用清单、任务与 Broker 编号映射、峰值/常驻显存测量、冷启动和热运行、视频先占卡、断线重启、取消、重复提交及最终译文去重测试
```

## 3. Manga / PanelTone

```text
请在 Manga / PanelTone 项目接入 AIALRA GPU Broker，保留现有持久清单、队列恢复、人工暂停和用户修改

配置 BROKER_BASE_URL=<BROKER_BASE_URL>、BROKER_PROJECT_ID=manga、BROKER_TOKEN=<PROJECT_TOKEN_SOURCE>，令牌只能由后端读取；按仓库 API.md 实现接口语义

审计当前启用的图像模型服务、每个处理单元的加载和推理、服务启动以及备用引擎；每个可能使用受管 GPU 的入口都必须受许可控制，未启用引擎在未来开启前也需校验

用持久清单中的稳定处理单元编号生成 external_id 和 idempotency_key，持久保存 broker_job_id、每个阶段的 request_key、permit_id 与后端编号；等待 ACTIVE 才调用模型，等待 GPU 与用户主动暂停分别显示

如果现有同步后端不能按编号查询或取消，先补查询/取消能力；响应丢失时无法确定是否执行则保持 NEEDS_RECOVERY 并阻止盲目重发；收到 CANCEL_REQUESTED 后按后端编号停止，确认不会继续使用 GPU 后才 finish permit

Broker 失联、401/403、遥测失效或许可非 ACTIVE 时停止新 GPU 阶段，不直连模型服务，不自动转到显示 GPU；模型常驻量要实测并上报

交付所有旁路清单、代码和配置、固定测试漫画及产物摘要、处理单元轮换、公平排队、响应丢失、重启恢复、取消、业务清单与 Broker 编号对账、显存峰值及回退步骤
```

## 4. 联合验收

三个项目各自完成后，统一检查受管 GPU 入口无绕过、同一时刻许可符合调度规则、实时会话状态准确、显示 GPU 无受管模型进程、失联时新准入冻结、等待与终态任务可对账、最终输出没有重复

至少执行固定音频、视频、漫画和桌面流畅度场景，保存去敏后的时间线、后端编号、显卡采样与输出摘要；只有联合验收通过后，管理员才从观察模式开启正式准入
