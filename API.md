# GPU Broker 接入协议 v1

同机地址：`http://127.0.0.1:18765`；跨主机 HTTPS 地址由部署者配置，当前实例为 `https://gpu.aialra.online`。请求头：`Authorization: Bearer <项目专用令牌>`；写入 JSON 时加 `Content-Type: application/json`。三个项目编号为 `minimax`、`live_translate`、`manga`。管理员令牌只用于监控和管理，项目进程不得持有。

项目令牌保存在部署者指定的数据目录 `tokens.json` 的 `projects` 下，只能由后端安全配置读取，不能放进网页。接口出现 401、403、409、5xx、超时或网络错误时，适配器必须停止启动新的 GPU 阶段，并在原业务系统保留任务和后端编号；不得按“Broker 不可用就直连 GPU”降级。

## 项目接口

| 接口 | 用途 | 必要字段 |
| --- | --- | --- |
| `POST /v1/projects/{project}/heartbeat` | 每 5 秒发送进程状态 | `instance_id`、`status`、`resident_mib` |
| `POST /v1/projects/{project}/jobs` | 幂等登记业务任务 | `external_id`、`idempotency_key`、`label` |
| `GET /v1/projects/{project}/jobs/{job_id}` | 查询任务及其许可 | 无 |
| `PATCH /v1/projects/{project}/jobs/{job_id}` | 上报业务状态 | `status`、可选 `backend_id`、`error_code` |
| `POST /v1/projects/{project}/sessions` | 申请实时会话或整项批处理任务保护 | `request_key`、`owner_instance`、可选 `kind`，值为 `realtime` 或 `batch_task`，默认 `realtime` |
| `GET /v1/projects/{project}/sessions/{session_id}` | 查询实时会话 | 无 |
| `POST /v1/projects/{project}/sessions/{session_id}/heartbeat` | 续期实时会话 | `owner_instance` |
| `POST /v1/projects/{project}/sessions/{session_id}/ready` | 模型预热并确认可用后上报 | `owner_instance` |
| `POST /v1/projects/{project}/sessions/{session_id}/close` | 后端确认停止后关闭 | `owner_instance`、`backend_confirmed_inactive:true` |
| `POST /v1/projects/{project}/permits` | 申请 GPU 阶段许可 | `job_id`、`profile_id`、`request_key`、`stage`、`owner_instance`、可选 `backend_id`、`session_id`；整项任务的各阶段共用同一个 `batch_task` 会话 |
| `GET /v1/projects/{project}/permits/{permit_id}` | 查询许可与等待原因 | 无 |
| `POST /v1/projects/{project}/permits/{permit_id}/heartbeat` | 每 5 秒续期活跃许可 | `owner_instance`、可选 `backend_id` |
| `POST /v1/projects/{project}/permits/{permit_id}/cancel` | 请求取消 | 无 |
| `POST /v1/projects/{project}/permits/{permit_id}/finish` | 后端停止后结束许可 | `owner_instance`、`result`、`backend_confirmed_inactive:true`、`resident_mib` |
| `POST /v1/projects/{project}/permits/{permit_id}/reconcile` | 本项目核销失联后的 `UNCERTAIN` 许可 | `backend_confirmed_inactive:true`、至少十字符的 `evidence`；须先按后端编号核对历史和运行／排队状态 |
| `POST /v1/projects/{project}/sessions/{session_id}/reconcile` | 本项目核销失联后的 `UNCERTAIN` 会话 | 同上；必须先核销关联的许可，确认整项任务的后端阶段均已停止 |

`jobs` 状态：`ACCEPTED`、`WAITING_GPU`、`RUNNING`、`COMMITTING`、`COMPLETED`、`FAILED`、`CANCEL_REQUESTED`、`CANCELLED`、`NEEDS_RECOVERY`。业务真相仍在原项目库，Broker 只是统一索引与审计。

`permits` 状态：`WAITING`、`ACTIVE`、`CANCEL_REQUESTED`、`UNCERTAIN`、`FINISHED`、`CANCELLED`。只有 `ACTIVE` 表示可启动 GPU 阶段。`CANCEL_REQUESTED` 时必须停止后端，Broker 仍视该许可为占用。`UNCERTAIN` 只能在核实后由管理员解除。

`sessions` 状态：`REQUESTED`、`PREPARING`、`READY`、`CLOSING`、`UNCERTAIN`、`CLOSED`。实时请求只有到 `PREPARING` 才开始预热；只有模型和端到端探针通过后才能报告 `READY`。

`batch_task` 是批处理业务任务的独占保护期，不是实时推理。它排到 `PREPARING` 后，项目确认自身任务入口准备就绪即可报告 `READY`；后续每个普通 GPU 阶段仍须单独申请并等到 `ACTIVE` 才能加载模型或提交工作流。整个任务完成前须持续续期会话，即使两阶段之间暂时没有活跃许可也不能释放；确认所有后端阶段停止后才能关闭。此类会话占用期间其他项目许可返回 `WAIT_TASK`，其阶段许可不再用静态显存增长估计作硬门槛，但仍要求遥测有效且同卡没有其他活跃许可或受保护会话。显存不足时项目只能停止并确认自己的阶段、保留任务与检查点再申请新阶段，不得抢占先到任务

项目令牌只能核销本项目的 `UNCERTAIN` 记录，Broker 不转发媒体，也无法独立验证 ComfyUI 历史；这里的 `evidence` 是项目后端的核对声明，必须由实现方在请求前以原后端编号查询历史及队列，不得仅凭 GPU 占用降低释放许可。核销后的许可结果为 `RECONCILED`，与正常 `COMPLETED` 不同；业务是否成功仍由原项目依据产物、任务编号和单次发布规则决定。若后端仍运行或结果不明，保持 `UNCERTAIN`，禁止启动后续 GPU 阶段

旧版未使用 `batch_task` 的客户端仍沿用原有画像门槛及许可流程；实时会话的预热规则不变。生产全局准入开关不会因本协议升级自动开启

典型等待原因：`WAIT_PAUSED`、`WAIT_RECONCILE`、`WAIT_TELEMETRY`、`WAIT_ACTIVE`、`WAIT_REALTIME`、`WAIT_TASK`、`WAIT_TASK_READY`、`WAIT_PROJECT_OFFLINE`、`WAIT_VRAM`、`PROFILE_NOT_FIT`。客户端应把原始代码和中文说明都保留在业务事件中，方便排障。

## 管理接口

管理员令牌可调用 `GET /v1/dashboard`、`GET /v1/history?gpu_uuid=...&minutes=60`、`GET /v1/events?after=0`、`GET /v1/admin/doctor`、`POST /v1/admin/backup`。`POST /v1/admin/allocation` 接受 `{"enabled":true|false}`。`POST /v1/admin/jobs/{job_id}/cancel` 会持久登记整个业务任务的取消请求，同时取消等待许可或将活跃许可改为 `CANCEL_REQUESTED`；适配器必须查询 job/permit 并真正停止后端。资源画像由 `POST /v1/profiles` 创建，字段为 `project_id`、`label`、`kind`（`batch` 或 `realtime`）、`peak_growth_mib`、`max_seconds`；`PATCH /v1/profiles/{id}` 可用 `{"enabled":false}` 停用。未完成许可关联的画像不能停用。

待核实许可使用 `POST /v1/admin/permits/{id}/reconcile`，待核实会话使用 `POST /v1/admin/sessions/{id}/reconcile`。请求必须包含 `{"backend_confirmed_inactive":true,"evidence":"至少十字符的具体核实证据"}`。管理页不会根据显卡占用降低就自动释放不确定任务；需要核对原后端任务编号和进程状态。

## 最小流程

```text
原项目持久保存用户任务与稳定外部编号
  → 项目心跳
  → Broker 登记 job，保存 broker_job_id
  → 为 GPU 阶段申请 permit，保存 permit_id
  → 等待 ACTIVE，期间持续项目心跳
  → 发送模型请求，保存预定后端编号
  → 持续项目与许可心跳
  → 后端明确完成／停止并核实不会继续计算
  → finish permit，更新业务任务终态
```

实时会话在申请许可之前插入 `request session → PREPARING → 模型预热 → ready`；处理完流式音频后确认模型卸载或后端停止，再 `close session`。已经运行的视频不能被安全地任意抢占，实时请求此时停在 `REQUESTED`。

同一业务任务重试应保持同一 `external_id` 和 `idempotency_key`；同一 GPU 阶段重试应保持同一 `request_key`。后端提交前先持久保存可查询的任务编号。如果请求已经送到后端而响应丢失，先按编号查询，不能直接再发一次。不同阶段必须用不同请求键。当前 Broker 不代替三个后端做结果去重；每个原项目还必须实现单次最终发布。
