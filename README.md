<div align="center">
<h1>AIALRA GPU Coordinator</h1>
<p>在一台 Windows 主机上观察计算显卡，并逐步接入跨项目所有权协调</p>
<p><a href="README.en.md">English</a> · <a href="API.md">接口协议</a> · <a href="INTEGRATION_PROMPTS.md">项目接入提示词</a> · <a href="DEPLOYMENT.md">部署说明</a></p>
</div>

当前已部署版本仍是旧 GPU Broker，负责原有任务准入与用量监控；控制台展示显卡和项目的运行状况

下一版正在收敛为 **4080 所有权协调器**
独立 Owner v1 只记录 `FREE / OWNED / UNKNOWN`，用 `acquire / release / observe` 协调 H3、Live、Manga；三项目继续用自己的队列和模型调用
已提交的计算不会因为协调器失联被取消；状态不明时新的 GPU 工作等待
公网控制台只用于观察，不进入本机 GPU 准入路径
设计见 [Owner v1](docs/OWNER_V1_DESIGN_2026-09-23.md)，本机双向签名接口见 [Owner API 合同](docs/OWNER_API_CONTRACT.md)

Owner v1 的代码与离线测试正在实施，**尚未启用三项目生产门禁**
下文的启动与许可说明描述当前旧 Broker，不应当作 Owner v1 的生产切换步骤

Owner 的独立 Windows 服务安装脚本位于 [Install-OwnerWinSWService.ps1](deploy/Install-OwnerWinSWService.ps1)，默认只登记手动启动服务
三项目 GPU 入口、模型释放和全空闲阈值完成真实验收后，才按 [部署门槛](DEPLOYMENT.md#owner-v1-部署门槛)启动并切换

Broker 目前预留了 `minimax`、`live_translate`、`manga` 三个项目身份和接口；项目侧接入及真实任务联合验收仍在进行，控制台心跳不能证明 GPU 调用已受控；其他项目需要扩展配置与接口校验；公开仓库不包含设备编号、访问令牌、任务数据和服务器配置

## 1 使用前提

- Windows、Python 3.12 或更新版本、NVIDIA 显卡及可用驱动
- 本机服务绑定 `127.0.0.1`，默认端口为 `18765`
- 一个进程管理一个 SQLite 数据库；不能用多个 Uvicorn worker 或自动重载运行调度器
- 初始化会生成管理员令牌和三个项目令牌，存放目录必须只允许受信任的本机账户读取
- 首次启动为观察模式，不会发放新的 GPU 许可

## 2 第一次运行

在本仓库根目录执行以下命令，先把 `<MANAGED_GPU_UUID>` 换成 `nvidia-smi -L` 显示的计算卡 UUID；显示卡参数可省略

```powershell
# 创建独立 Python 环境并安装服务
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"

# 只初始化一次，令牌不会在终端打印
.\.venv\Scripts\python.exe -m gpu_broker init --data-dir "$env:LOCALAPPDATA\AIALRA\GpuBroker" --managed-gpu-uuid '<MANAGED_GPU_UUID>'

# 启动单实例服务
.\.venv\Scripts\python.exe -m gpu_broker serve --data-dir "$env:LOCALAPPDATA\AIALRA\GpuBroker"
```

打开 `http://127.0.0.1:18765/`，从数据目录的 `tokens.json` 读取 `admin` 字段并在控制台输入；这个令牌只属于管理员，项目进程只能读取 `projects.<项目编号>` 对应的令牌

浏览器显示遥测和“观察模式”即为第一次运行成功；若遥测失效，服务会停止发放新许可，已登记任务仍保留在数据库中等待核实

## 3 项目怎样获得许可

项目先持久保存自己的任务编号，再调用 Broker 登记 job 和申请 permit；只有 permit 状态为 `ACTIVE` 才启动模型加载或推理

运行期间项目发送心跳，结束时先确认后端已经停止，再结束 permit；失联、取消中或状态不确定时，Broker 保留占用记录并冻结可能冲突的新许可

`heartbeat_timeout_seconds` 用于判断项目在线状态，默认 15 秒；活跃 permit 和已就绪会话还会等待 `active_heartbeat_grace_seconds`，默认额外 180 秒；因此 195 秒内的短暂心跳中断不会被误判为失联，原许可仍占用 GPU

超过该时长仍没有心跳才会进入 `UNCERTAIN` 并冻结新准入，须核实后端后再恢复；准备中的会话仍受独立的 `session_prepare_timeout_seconds` 限制

完整字段、状态和错误处理见 [接口协议](API.md)，三个现有项目的实施要求见 [项目接入提示词](INTEGRATION_PROMPTS.md)

## 4 公网访问与边界

[已部署的控制台](https://gpu.aialra.online/) 经过 Authentik 登录保护；控制台仍要求单独输入管理员令牌

项目进程可以通过 HTTPS 调用 `/v1/projects/{project_id}/...`，请求必须携带该项目的 Bearer 令牌；本机同机项目优先使用回环地址，跨主机项目才使用公网地址

公网网关在服务端校验认证并通过私有连接转发到本机；不要把管理员令牌、项目令牌或本机数据库放进前端代码、Git 仓库或网站卡片

部署路径、认证规则和故障检查见 [部署说明](DEPLOYMENT.md)

## 5 验证与限制

```powershell
# 运行调度和接口测试
.\.venv\Scripts\python.exe -m pytest -q

# 检查本机 GPU 与数据库
.\.venv\Scripts\python.exe -m gpu_broker doctor --data-dir "$env:LOCALAPPDATA\AIALRA\GpuBroker"
```

测试使用模拟 GPU；真实项目接入还需核查每一条模型加载路径、重复提交、取消、后端失联、重启恢复和显存峰值

软件不能保证硬件、驱动、网络或电源绝对稳定；在三项目完成联合验收前，应保持观察模式

## 6 许可与反馈

仓库目前没有开源许可证，公开可见不等于授权复制、修改或再分发；如需复用，请先联系维护者确认许可

问题可通过本仓库的 GitHub Issues 报告，报告中请删除令牌、设备 UUID、真实任务内容和私人网络信息
