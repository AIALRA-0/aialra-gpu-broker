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
| `POST /v1/projects/{project}/sessions` | 申请实时保护 | `request_key`、`owner_instance` |
| `GET /v1/projects/{project}/sessions/{session_id}` | 查询实时会话 | 无 |
| `POST /v1/projects/{project}/sessions/{session_id}/heartbeat` | 续期实时会话 | `owner_instance` |
| `POST /v1/projects/{project}/sessions/{session_id}/ready` | 模型预热并确认可用后上报 | `owner_instance` |
| `POST /v1/projects/{project}/sessions/{session_id}/close` | 后端确认停止后关闭 | `owner_instance`、`backend_confirmed_inactive:true` |
| `POST /v1/projects/{project}/permits` | 申请 GPU 阶段许可 | `job_id`、`profile_id`、`request_key`、`stage`、`owner_instance`、可选 `backend_id`、`session_id` |
| `GET /v1/projects/{project}/permits/{permit_id}` | 查询许可与等待原因 | 无 |
| `POST /v1/projects/{project}/permits/{permit_id}/heartbeat` | 每 5 秒续期活跃许可 | `owner_instance`、可选 `backend_id` |
| `POST /v1/projects/{project}/permits/{permit_id}/cancel` | 请求取消 | 无 |
| `POST /v1/projects/{project}/permits/{permit_id}/finish` | 后端停止后结束许可 | `owner_instance`、`result`、`backend_confirmed_inactive:true`、`resident_mib` |

`jobs` 状态：`ACCEPTED`、`WAITING_GPU`、`RUNNING`、`COMMITTING`、`COMPLETED`、`FAILED`、`CANCEL_REQUESTED`、`CANCELLED`、`NEEDS_RECOVERY`。业务真相仍在原项目库，Broker 只是统一索引与审计。

`permits` 状态：`WAITING`、`ACTIVE`、`CANCEL_REQUESTED`、`UNCERTAIN`、`FINISHED`、`CANCELLED`。只有 `ACTIVE` 表示可启动 GPU 阶段。`CANCEL_REQUESTED` 时必须停止后端，Broker 仍视该许可为占用。`UNCERTAIN` 只能在核实后由管理员解除。

`sessions` 状态：`REQUESTED`、`PREPARING`、`READY`、`CLOSING`、`UNCERTAIN`、`CLOSED`。实时请求只有到 `PREPARING` 才开始预热；只有模型和端到端探针通过后才能报告 `READY`。

典型等待原因：`WAIT_PAUSED`、`WAIT_RECONCILE`、`WAIT_TELEMETRY`、`WAIT_ACTIVE`、`WAIT_REALTIME`、`WAIT_PROJECT_OFFLINE`、`WAIT_VRAM`、`PROFILE_NOT_FIT`。客户端应把原始代码和中文说明都保留在业务事件中，方便排障。

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
