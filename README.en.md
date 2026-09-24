<div align="center">
<h1>AIALRA GPU Coordinator</h1>
<p>Observe a Windows compute GPU and introduce cross-project ownership coordination</p>
<p><a href="README.md">简体中文</a> · <a href="API.md">API</a> · <a href="INTEGRATION_PROMPTS.md">Integration prompts</a> · <a href="DEPLOYMENT.md">Deployment</a></p>
</div>

The currently deployed broker records jobs, resource profiles, permits, realtime sessions, and recovery decisions. Its local dashboard shows GPU telemetry, queue reasons, project heartbeats, job states, and events.

The next version is being reduced to a **4080 ownership coordinator**. Its independent Owner v1 records only `FREE / OWNED / UNKNOWN` and coordinates H3, Live, and Manga through `acquire / release / observe`. Each project keeps its own queue and model calls. A coordinator outage cannot cancel submitted compute; uncertain ownership blocks new GPU work. The public dashboard is observational and outside the local admission path. See the [Owner v1 design](docs/OWNER_V1_DESIGN_2026-09-23.md) and [signed local API contract](docs/OWNER_API_CONTRACT.md).

Owner v1 is under implementation and offline testing. **It is not the production gate for the three projects yet.** The startup and permit instructions below describe the existing broker, not an Owner v1 cutover.

The separate [Owner Windows service installer](deploy/Install-OwnerWinSWService.ps1) registers a manually started service by default. Start and cut over only after all three GPU entry gates, model release paths, and a measured idle threshold pass real-task acceptance; see the [deployment gates](DEPLOYMENT.md#owner-v1-部署门槛).

The broker currently reserves project identities and API routes for `minimax`, `live_translate`, and `manga`. Their client integrations and real-task acceptance are still in progress; a dashboard heartbeat does not prove that GPU calls are gated. Other projects need corresponding configuration and API validation changes. Device identifiers, credentials, job data, and private server configuration are not part of this repository.

## 1 Requirements

- Windows, Python 3.12 or newer, and an NVIDIA GPU with a working driver
- One server process bound to `127.0.0.1`, using port `18765` by default
- One process per SQLite database; do not enable multiple Uvicorn workers or auto reload
- A data directory readable only by trusted local accounts
- Admission starts paused in observation mode

## 2 First run

From the repository root, replace `<MANAGED_GPU_UUID>` with the compute GPU UUID shown by `nvidia-smi -L`.

```powershell
# Create an isolated Python environment
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"

# Initialize once; tokens are written to disk and are not printed
.\.venv\Scripts\python.exe -m gpu_broker init --data-dir "$env:LOCALAPPDATA\AIALRA\GpuBroker" --managed-gpu-uuid '<MANAGED_GPU_UUID>'

# Run the single server process
.\.venv\Scripts\python.exe -m gpu_broker serve --data-dir "$env:LOCALAPPDATA\AIALRA\GpuBroker"
```

Open `http://127.0.0.1:18765/` and enter the `admin` value from the data directory's `tokens.json`. Each project must use only its own `projects.<project_id>` value.

The dashboard should show telemetry and observation mode. When telemetry is stale, new permits are blocked while existing records remain available for recovery.

## 3 Admission flow

A project persists its own job ID, registers a broker job, and requests a permit. It may start GPU work only after the permit reaches `ACTIVE`.

While working, it sends heartbeats. To finish, it verifies that the backend is inactive before closing the permit. Lost heartbeats and uncertain state retain the reservation until reconciliation.

`heartbeat_timeout_seconds` checks whether a project is online and defaults to 15 seconds. Active permits and ready sessions also use `active_heartbeat_grace_seconds`, which defaults to an additional 180 seconds. A brief heartbeat outage within the resulting 195-second window does not mark active work as lost or release its GPU reservation. A longer outage moves the record to `UNCERTAIN`, freezes new admission, and requires backend reconciliation. Preparing sessions also retain their separate `session_prepare_timeout_seconds` limit.

See the [API reference](API.md) for fields and states, and the [integration prompts](INTEGRATION_PROMPTS.md) for the three target projects.

## 4 Public access

The [deployed dashboard](https://gpu.aialra.online/) requires Authentik sign in and a separate broker administrator token. Project clients can call `/v1/projects/{project_id}/...` over HTTPS using their project Bearer token. Clients on the same host should prefer the loopback URL.

The public gateway forwards requests over a private connection. Never put administrator or project tokens, the SQLite database, or device identifiers in browser code or a Git repository. See [deployment](DEPLOYMENT.md) for the gateway and recovery procedure.

## 5 Verification and limits

```powershell
# Run scheduler and API tests
.\.venv\Scripts\python.exe -m pytest -q

# Check GPU telemetry and database integrity
.\.venv\Scripts\python.exe -m gpu_broker doctor --data-dir "$env:LOCALAPPDATA\AIALRA\GpuBroker"
```

Tests use a simulated GPU. Real integration still requires checking every model entry point, duplicate submissions, cancellation, backend outages, restart recovery, and measured memory peaks. Hardware, drivers, networking, and power cannot be guaranteed by this software. Keep admission paused until all three integrations pass joint acceptance.

## 6 License and support

No open source license has been granted yet. Public visibility alone does not grant permission to copy, modify, or redistribute the code. Please contact the maintainer before reuse.

Report issues through GitHub Issues after removing tokens, GPU UUIDs, private job data, and internal network details.
