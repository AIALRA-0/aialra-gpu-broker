# Owner v1 本机观察接口

**状态：Coordinator 接口与离线测试已实现；三个项目的适配器、空载阈值和生产切换尚未完成。**

Coordinator 只调用事先配置的回环地址。项目必须在收到每次请求后重新检查自己的 GPU 入口、后端活动、子进程和模型驻留；不能返回上次缓存的“空闲”。公开网站不能访问此接口。详细状态机见 [Owner v1 设计稿](OWNER_V1_DESIGN_2026-09-23.md)。

## 请求与响应

```http
GET /internal/gpu/owner-observe?nonce=<随机挑战>
```

```json
{
  "nonce": "<原样回传本次挑战>",
  "project": "h3",
  "status": "BUSY",
  "observed_at": 1790220000.123456,
  "owner_instance": "<当前窗口的不透明 ID，空闲时可为 null>",
  "model_released": false,
  "child_processes_exited": false,
  "entry_fenced": false,
  "signature": "<HMAC-SHA256 十六进制>"
}
```

`project` 仅能是 `h3`、`live`、`manga`。`status` 仅能是 `BUSY`、`IDLE`、`UNKNOWN`。`observed_at` 用本机 `time.time()` 的浮点秒数，在实际采样后立即生成，连续请求不能重复旧时间戳。任一事实无法核实就返回 `UNKNOWN`，不要猜 `IDLE`。

签名输入是**删除 `signature` 字段后的整个 JSON 对象**，按键排序，使用 `json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")` 得到字节，再用本项目凭据计算 HMAC-SHA256 十六进制。Coordinator 不在请求中发送凭据，核对挑战、签名、时间和整卡采样。凭据不进入响应、日志或网页。此签名只证明观察端身份，不是每次模型调用的许可。

`IDLE` 是项目观察端对本项目已经可以交接的整体确认：本窗口入口已封闭，旧 `owner_instance` 不会再发起下一 GPU 阶段；后端没有未完成 GPU 调用；本项目模型已释放或受控进程已确认不会继续占用 4080。`entry_fenced` 仍须明确为 `true`；`model_released` 和 `child_processes_exited` 是可选诊断细项，不要求三个项目都提供同一种证明，但已提供的 `false` 会与 `IDLE` 矛盾并阻止交接。无法确认整体 `IDLE` 时返回 `UNKNOWN`，不能靠留空诊断字段猜测空闲。H3 视频两个阶段之间即使显存暂低，也要报告 `BUSY`；Live 队列中尚未领取的任务可以等待下一窗口，但当前窗口不能在释放后继续领任务；Manga 导入线程和生成队列须共用同一入口封闭判断。

项目主动 `release` 之前先封闭该窗口入口，再等待后端停止并卸载。Coordinator 收到 release 后会重新调用三个项目的观察接口并读取整卡；只有全部直接事实为安全空闲，才会返回 `RELEASED`。观察超时或矛盾时返回 `UNKNOWN`，不会取消已有计算。

Owner 服务使用单独的 [本机 HTTP 应用](../gpu_broker/owner_api.py)，CLI 只绑定 `127.0.0.1`，默认端口 `18767`；原公开 Broker 与隧道不能转发其修改接口。运行配置 `owner.json` 必须显式提供 GPU UUID、经过全空闲实测的 `idle_max_mib`、`idle_max_utilization_pct` 和三个回环观察 URL；缺少配置时服务拒绝启动。项目凭据从受限的现有 `tokens.json` 读取，接口身份映射是旧 `minimax→h3`、`live_translate→live`、`manga→manga`，不复用旧 Broker 的任务表和许可状态。

独立 Owner 进程每 5 秒尝试一次直接观察，以免长任务期间没有项目主动调用 `observe()` 而使仪表盘证据过期。轮询只读取项目与整卡事实并更新三态；观察失败会使新的准入等待或让证据过期，不会向 ComfyUI、Live Worker 或 Manga 模型服务发送取消命令。手动 `acquire` 与 `release` 仍各自重新采集事实，不能使用轮询缓存直接放行或释放。
