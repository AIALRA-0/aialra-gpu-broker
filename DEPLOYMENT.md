# 部署与运行说明

此文档描述当前实例采用的网络边界和通用部署步骤，主机名、端口、用户、证书路径与身份网关路径都要按自己的环境设置

> **当前旧 Broker 部署记录。** 下文的 `ACTIVE` permit、项目 Bearer 令牌和公网项目 API 描述现有服务，不再是三项目目标架构。正在实施的 Owner v1 仅通过本机 `127.0.0.1:18767` 的双向签名接口协调所有权；公网 `gpu.aialra.online` 只提供观察和管理。生产切换仍未进行，详见 [Owner v1 设计](docs/OWNER_V1_DESIGN_2026-09-23.md) 与 [本机接口合同](docs/OWNER_API_CONTRACT.md)

## Owner v1 部署门槛

Owner 使用独立进程、独立 `owner.sqlite3` 和本机端口 18767。`owner.json` 要填入实际 4080 UUID、在三项目与模型后端都确认空闲后测得的显存及利用率交接范围，以及三项目的回环观察 URL；缺项时启动会失败。可先运行 `.\.venv\Scripts\python.exe -m gpu_broker.owner_cli check-config --data-dir <受限数据目录>` 检查结构与凭据而不创建 Owner 数据库、不连接项目、不输出令牌。安装脚本 [Install-OwnerWinSWService.ps1](deploy/Install-OwnerWinSWService.ps1) 也会先执行该检查，默认只登记手动启动的 Windows 服务，不会启动或切换生产调用。脚本参数为 WinSW 可执行文件、仓库目录与受限数据目录，可指定端口；`-Start` 只在全部项目入口和观察端已通过验收后使用。`/v1/health` 只表示进程存活，不能当作 `FREE` 或真实任务通过的证明

首轮切换需要先让 H3、Live、Manga 的 GPU 取队列暂停，确认它们和后端都已停止并卸载，验证各 GPU 入口在门禁关闭时拒绝新提交，然后启动 Owner、观察到有直接事实支持的 `FREE`，再依次恢复项目队列。按 [Owner v1 最小验收](docs/OWNER_ACCEPTANCE_MATRIX.md) 完成真实 H3 视频、Live 录音后 GPU 阶段、Manga 单页及一次运行中故障恢复；每迁移一个项目便删除其旧 Broker 准入强依赖。完成这些真实闭环后，才能把 Owner 服务启动类型设为自动。切换时不导入旧 Broker 的 session、job 或 permit；公网隧道与 Authentik 仍只连接仪表盘端口 18765

## 1. 旧 Broker 未实施的共享请求路径

以下是旧共享 Broker 的历史方案，不是 Owner v1 的部署步骤，也不是三项目已启用的调用路径
该方案曾要求项目后端向同一个本机 Broker（默认地址为 `http://127.0.0.1:18765`）申请受管 GPU 阶段许可；只有收到 `ACTIVE` 才调用模型服务
模型输入与结果仍由各项目后端和本机模型服务传递，任务编号、许可和状态才进入旧 Broker；等待、拒绝或无法连接时不能直连模型作为回退

```text
浏览器 → HTTPS 反向代理 → Authentik 登录校验 → 私有转发 → 本机 Broker
项目后端 → HTTPS 反向代理 → 项目 Bearer 令牌校验 → 私有转发 → 本机 Broker
同机项目后端 → 本机共享 Broker
```

浏览器页面必须同时通过 Authentik 登录和 Broker 管理员令牌；项目 API 只接受项目自己的 Bearer 令牌，不能接受管理员令牌作为项目配置

私有转发当前由本机主动建立 SSH 反向连接，远端监听仅绑定 `127.0.0.1`；公网服务器不能直接连接本机数据库或 GPU 端口

该旧方案未成为三项目共享生产准入：H3 仍接入自己的 Broker，Live 的 `AIALRA_GPU_BROKER_ENABLED` 与 Manga 的 `PANELTONE_GPU_BROKER_MODE` 均保持关闭；共享实例的全局 `allocation_enabled` 也保持关闭，因此它不会发放 `ACTIVE` 许可
三项目现状与实施中的共享门禁决策见 [GPU 调用复核](GPU_CALL_REVIEW_2026-09-22.md)

## 2. 本机准备

先按 [README](README.md) 初始化数据目录并完成本机测试，再使用 `deploy/Protect-Tokens.ps1` 限制令牌文件权限

在 `config.json` 中设置 `public_origin` 为浏览器实际使用的 HTTPS 源地址，例如 `https://gpu.example.org`；它只用于校验写请求的 `Origin`，不能包含路径或末尾斜杠

服务应始终绑定 `127.0.0.1`，只启动一个 worker，关闭自动重载；需要开机后自动恢复时，优先使用受控 Windows 服务，并检查服务账户对 GPU 驱动和数据目录的访问权限

当前仓库还提供用户登录启动任务；这种模式只有账户登录后才启动，不能替代开机自启服务

登录任务中的启动脚本会在 Python 进程意外退出后持续重试，每次间隔默认 10 秒；计划任务也配置失败重启。部署后须分别监视本机 `/v1/health`、公网 `/ready` 和一条经认证的实际业务请求。当前公网 `/ready` 经 Nginx 和私有隧道转发到 Broker 的 `/v1/ready`；200 表示该次请求抵达 Broker，且 GPU 遥测及受管卡信息满足就绪检查，不能证明 H3、Live、Manga 的业务任务或模型调用成功。进程重启、主机内存紧张或隧道中断期间仍可能短暂不可用

Windows 版启动脚本把监护进程及其 Python 子进程放入同一个 Job Object；停止计划任务时应同时释放监听端口。可运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_run_gpu_broker_supervisor_job.ps1` 做不连接 GPU 的进程归属测试

## 3. 私有转发

在本机 SSH 配置中为网关创建专用别名和受限密钥，确保远端反向端口只绑定回环地址；检查远端监听地址确实是 `127.0.0.1`，不能是 `0.0.0.0`

`deploy/Install-PrivateTunnel.ps1` 可以在用户登录时建立转发，并在 SSH 断线后重试；运行时按实际别名、远端端口、本机端口传参

```powershell
# 将示例远端端口替换为网关实际分配的端口
.\deploy\Install-PrivateTunnel.ps1 -SshHost 'gpu-gateway' -RemotePort 29000 -LocalPort 18765
```

网关上向 `http://127.0.0.1:<REMOTE_PORT>/v1/health` 发请求应获得 `running=true`；若失败，先检查本机服务，再检查 SSH 转发任务和远端端口监听

## 4. HTTPS 与认证网关

给公开域名配置 DNS、有效证书和 HTTPS；反向代理上设短连接超时、请求体上限及必要的速率限制

- `/`、`/static/`、`/v1/dashboard`、`/v1/history`、`/v1/events` 和所有管理接口先通过 Authentik 校验，再由 Broker 校验管理员令牌
- `/v1/projects/` 可由机器后端访问，反向代理不要求浏览器登录，但 Broker 必须校验项目 Bearer 令牌和项目编号
- 代理应清除客户端伪造的内部身份头，拒绝非预期路径，并避免把令牌写入访问日志
- Authentik 应登记新域名的回调地址和对应访问组，未授权用户不能进入浏览器控制台

当前实例使用 `gpu.aialra.online`，主站上的卡片只保存公开地址，不保存任何令牌；现有 `/ready` 会访问本机 Broker 的就绪接口，卡片可据此显示 Broker 与 GPU 遥测就绪，但不能凭它宣告三项目业务正常

网关和隧道连通性、本机 Broker 健康、三项目真实业务完成须分别核查；`/ready` 覆盖前两者的一部分，不能代替完成 Authentik 登录后的业务请求验收

## 5. 验证与故障演练

上线前依次核查：本机健康接口、网关私有端口、公网证书、匿名浏览器被引导登录、无令牌项目 API 返回 401、错误项目令牌返回 403、正确项目令牌只访问本项目、跨域写请求被拒绝

完成后测试 SSH 中断、Broker 重启、NVML 遥测中断、任务取消与失联恢复；新许可在任何不确定状态下必须停止发放，已有后端任务要通过后端编号核实，不能仅凭显存下降解除冻结

旧公网 Broker 出现 502 时依次检查本机 `http://127.0.0.1:18765/v1/health`、本机登录任务、反向隧道任务和网关私有端口。历史故障中隧道仍在运行，而本机 Broker 已退出；仅重启网关不会修复失效的本机监听。该子进程退出的底层诱因尚未确认，作为旧服务运行问题记录；Owner v1 的本机准入不经过此公网链路，不以旧 Broker 的 502 复验作为上线条件

为了降低负载，浏览器在可见时每 5 秒读取一次总览，历史数据最多每 15 秒读取一次；页面隐藏时暂停定时查询，服务端遥测采样默认每 2 秒一次

## 6. 稳定性边界

当前本机用户任务和 SSH 任务依赖账户登录，注销后服务或转发可能停止；正式无人值守运行需要管理员权限安装并验证 Windows 服务，完成系统重启、断网、驱动故障和长时间运行测试

软件只能在遥测和任务状态不确定时停止发放新许可，无法保证硬件、驱动、操作系统、网络和电源绝对无故障；观察模式应保持到三个项目全部完成联合验收
