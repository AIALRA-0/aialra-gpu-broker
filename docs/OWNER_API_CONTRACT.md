# Owner v1 本机接口合同

本合同描述正在实施的独立 Owner API，尚不是三项目生产切换指令。服务只绑定 `127.0.0.1:18767`，网站和公网隧道不代理本页的获取、观察或释放接口。项目保留自身队列、模型调用与业务状态

## 接口与状态

| 方法与路径 | 请求体 | 含义 |
| --- | --- | --- |
| `GET /v1/owner` | 无 | 只读登记摘要，不触发新观察；证据过期时签名返回 `UNKNOWN`，保留最后 Owner 身份供核查，不据此改变持久状态或允许 GPU 提交 |
| `POST /v1/owner/observe` | 无 | 直接采集三项目与整卡事实，返回 `FREE`、`OWNED` 或 `UNKNOWN` |
| `POST /v1/owner/acquire` | `{"owner_instance":"..."}` | 申请独占；同一随机实例 ID 用于安全重试 |
| `POST /v1/owner/release` | `{"owner_instance":"..."}` | 声明本项目已可交接；服务仍会直接核实，不能仅凭声明释放 |

项目只能用自己的身份执行修改操作。`GET` 可使用项目或只读管理员身份。`/v1/health` 仅证明进程存活，不证明可获取显卡。`WAITING`、`UNKNOWN`、无响应和签名验证失败均不得启动新的 GPU 工作。已提交的 GPU 计算不因这些故障被 Owner 取消

## 双向签名

项目凭据留在项目本机受限文件中。Owner 请求不发送 Bearer 令牌；请求头如下：

```text
X-Owner-Project: h3 | live | manga | admin
X-Owner-Nonce: 每次请求独立生成的 URL 安全随机字符串
X-Owner-Timestamp: 当前 Unix 秒数字符串
X-Owner-Signature: 十六进制 HMAC-SHA256
```

请求签名密钥是该项目自己的令牌。被签名的对象是下列 JSON，按键名排序，以 UTF-8 编码且不加多余空格；`timestamp` 在对象中是数字，`method` 使用大写，`path` 不含域名和查询参数；无请求体时 `body` 为 `{}`

```json
{"body":{"owner_instance":"<随机实例 ID>"},"method":"POST","nonce":"<随机挑战>","path":"/v1/owner/acquire","project":"h3","timestamp":1234567890.25}
```

即 `HMAC_SHA256(project_token, json.dumps(object, sort_keys=True, separators=(",", ":")).encode("utf-8"))` 的小写十六进制值。服务检查签名、时间窗口及随机挑战重放

成功响应采用 `{"nonce":"...","result":{...},"signature":"..."}`。响应签名使用同一项目令牌，对按相同规则序列化的 `{"nonce":"...","result":{...}}` 求 HMAC。客户端必须先核对响应挑战等于本次请求挑战，再比较签名，最后才解析 `result`。即使 `result` 声称 `ACQUIRED`，挑战或签名错误也不得放行

签名解决本机 Owner 端口被错误进程抢占时伪造许可或套取明文令牌的问题，但持有项目令牌的同账户恶意进程仍不在这一机制的防护范围。生产接入还须限制令牌文件权限、固定回环地址，并在三项目入口全部安装门禁后进行空闲切换

项目观察端的反向随机挑战与签名见 [Owner 观察接口合同](OWNER_OBSERVER_CONTRACT.md)。实现及三态恢复规则见 [Owner v1 设计](OWNER_V1_DESIGN_2026-09-23.md)
