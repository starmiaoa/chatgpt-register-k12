# chatgpt-register-sub2api

`chatgpt-register-sub2api` 是一个本地 WebUI 工具，用于管理邮箱池、注册或登录 ChatGPT 账号、刷新账号工作空间上下文，并导出多种兼容 JSON。默认导出 Sub2API，也可以切换为 auth.json、CPA、Cockpit、9router、AxonHub 等格式。

项目支持 Outlook/Gmail OAuth 邮箱池、Outlook plus alias、workspace/K12 上下文检查、已有账号邮箱验证码登录、并发执行和结果归档。仓库中的配置均为脱敏示例，不包含真实邮箱、密码、token、workspace ID 或运行结果。

> 请仅在你有权使用的邮箱、账号和工作空间中运行本工具，并自行确认相关服务条款和合规要求。

## 功能特性

- 支持 Outlook OAuth 邮箱池接收验证码
- 支持 Gmail OAuth/IMAP 邮箱池接收验证码
- 支持 Outlook `+数字` 别名扩展
- 支持注册、workspace 加入/申请、refresh/check、导出完整流水线
- 支持本地 WebUI 两种主流程
- 支持已有账号邮箱验证码登录并重新获取 token
- 支持 K12/workspace 上下文识别，避免把未确认的 personal/free 上下文误导出
- 支持按 `workspace_id + email` 防重复，同一邮箱可复用到不同 workspace
- 支持注册、加入、刷新等阶段并发配置
- 支持按运行时间和账号数量归档输出文件
- 支持多种 JSON 导出格式，默认 Sub2API

## 工作流程

```text
邮箱池 -> 注册新账号或验证码登录已有账号 -> 加入/申请 workspace -> refresh/check 账号上下文 -> 健康检查 -> 导出 JSON
```

完整运行后，默认会在 `runs/` 下生成独立目录：

```text
runs/
  20260706-093012_6_accounts/
    registered_accounts.json
    sub2api_bundle.json
    test_run.log
```

跨运行共享的状态文件保存在：

```text
data/outlook_token_state.json
data/workspace_account_state.json
```

`outlook_token_state.json` 记录邮箱凭据是否失效和临时占用。`workspace_account_state.json` 按 `workspace_id + email` 记录是否已处理、检查或导出。同一邮箱已经导出到 workspace A，不会影响它继续导出到 workspace B。

## 安装

```bash
git clone <this-repo>
cd chatgpt-register-sub2api
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell：

```powershell
git clone <this-repo>
cd chatgpt-register-sub2api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

依赖：

- `curl-cffi`
- `pyyaml`

## 快速开始：本地 WebUI

生成本地配置文件：

```bash
chatgpt-register init
```

启动 WebUI：

```bash
chatgpt-register web -c config.yaml --host 127.0.0.1 --port 8787
```

打开：

```text
http://127.0.0.1:8787/
```

在页面里填写或保存：

- 邮箱池
- 代理
- workspace ID
- 导出格式
- 数量和线程
- Outlook 别名设置

页面提供两个主按钮：

```text
无账号注册
有账号登录获得新的 token
```

`无账号注册` 会执行注册、join、refresh/check 和导出。`有账号登录获得新的 token` 会用邮箱验证码登录已有账号，重新获取 token 后执行 join、refresh/check 和导出。导出格式默认是 Sub2API，可在页面的“导出格式”下拉框切换。

输出文件会进入：

```text
runs/YYYYMMDD-HHMMSS_<count>_accounts/
```

## 命令行用法

旧 CLI 仍然可用。执行完整流水线：

```bash
chatgpt-register run -c config.yaml -n 6 -t 3 --workspace-id <workspace-uuid> -v
```

参数说明：

- `-n 6`：本次注册 6 个账号
- `-t 3`：每个阶段最多使用 3 个 worker
- `--workspace-id`：目标 workspace UUID
- `-v`：输出详细日志

## 最小示例：1 个 Outlook 邮箱注册 6 个账号

Outlook 支持 plus alias。开启别名后，一个主邮箱可以依次用于：

```text
user@example.com
user+1@example.com
user+2@example.com
user+3@example.com
user+4@example.com
```

配置示例：

```yaml
mail:
  providers:
    - type: outlook_token
      enable: true
      mode: auto
      alias_enabled: true
      alias_limit_per_mailbox: 5
      mailboxes: |
        user@example.com----mail_password----client_id----refresh_token

proxy:
  url: "socks5://127.0.0.1:10808"

workspace:
  enabled: true
  ids:
    - "your-workspace-uuid"
  route: k12_request
  re_login_enabled: false
```

运行：

```bash
chatgpt-register run -c config.yaml -n 5 -t 3 --workspace-id <workspace-uuid> -v
```

结果文件位于：

```text
runs/YYYYMMDD-HHMMSS_5_accounts/sub2api_bundle.json
```

## 导出格式

默认格式是 `sub2api`。支持的格式：

- `sub2api`：Sub2API bundle，默认文件名 `sub2api_bundle.json`
- `auth`：Codex/ChatGPT auth.json 风格，默认文件名 `auth.json`
- `raw-session`：Session JSON 风格，默认文件名 `session.json`
- `cpa`：CPA 风格账号数组，默认文件名 `cpa.json`
- `cockpit`：Cockpit 风格账号数组，默认文件名 `cockpit.json`
- `9router`：9router 风格账号数组，默认文件名 `9router.json`
- `axonhub`：AxonHub 风格账号数组，默认文件名 `axonhub-auth.json`

配置文件里可以这样固定导出格式：

```yaml
export:
  format: sub2api
  output_file: ""
```

`output_file` 留空时会按格式自动选择默认文件名。为了兼容旧配置，`sub2api.output_file` 仍然对 Sub2API 格式有效。

CLI 临时切换格式：

```bash
chatgpt-register run -c config.yaml -n 5 -t 3 --workspace-id <workspace-uuid> --format cpa -v
chatgpt-register export -c config.yaml -i registered_accounts.json --format 9router -v
```

## 配置说明

完整示例见 `config.example.yaml`。下面是主要配置项。

### 邮箱池

Outlook token 池格式：

```text
email----password----client_id----refresh_token
```

Gmail OAuth 池格式：

```text
email----client_id----client_secret----refresh_token
```

### Outlook 别名

```yaml
alias_enabled: true
alias_limit_per_mailbox: 5
```

含义：

- `alias_enabled`：是否启用 Outlook plus alias
- `alias_limit_per_mailbox`：每个主邮箱最多使用多少个注册地址，包含主邮箱本身

验证码仍从主 Outlook 邮箱读取；注册邮箱地址则按具体别名区分。

默认建议每个主邮箱使用 5 个地址（主邮箱 + `+1` 到 `+4`）。如果你明确要尝试第 6 个地址，可以把 `alias_limit_per_mailbox` 改成 `6`，但 Outlook/目标服务对第 6 个别名更容易出现验证码超时或后续 token 吊销。

### Workspace

```yaml
workspace:
  enabled: true
  ids:
    - "your-workspace-uuid"
  route: k12_request
  re_login_enabled: false
  export_plan_type: k12
```

常用 route：

- `accept`
- `request`
- `k12_request`

默认推荐通过 refresh/check 确认账号当前 workspace 上下文，再导出 Sub2API JSON。

### 导出健康检查

```yaml
sub2api:
  require_team_tokens: auto
  health_check: true
  health_check_endpoint: models
  health_check_retries: 2
  health_check_delay_seconds: 5
```

`health_check` 开启后，导出前会用 ChatGPT backend 做一次轻量检查。已经被服务端吊销、返回 `token_invalidated`、或检查接口异常的账号不会写入最终导出 JSON。因此导出数量可能少于注册成功数量，这是为了避免导入后立刻 401。

### 输出归档

```yaml
output:
  archive_runs: true
  runs_dir: runs
```

开启后，完整 `run` 会将本次结果写入独立目录，避免根目录堆积运行结果。

## 命令

| 命令 | 说明 |
| --- | --- |
| `web` | 启动本地 WebUI |
| `init` | 生成默认 `config.yaml` |
| `register` | 只执行注册 |
| `join-workspace` | 对已有账号执行 workspace 加入/申请 |
| `refresh` | 刷新 token 并检查账号/workspace 上下文 |
| `login-team` | 实验性 team/workspace 重新登录流程 |
| `export` | 将已有账号记录导出为指定格式 JSON |
| `run` | 完整流水线：register -> join -> refresh/check -> export |

示例：

```bash
chatgpt-register web -c config.yaml --host 127.0.0.1 --port 8787
chatgpt-register register -c config.yaml -n 5 -t 3 -v
chatgpt-register join-workspace -c config.yaml -i registered_accounts.json --workspace-id <workspace-uuid> -t 5 -v
chatgpt-register refresh -c config.yaml -i registered_accounts.json --workspace-id <workspace-uuid> -t 5 -v
chatgpt-register export -c config.yaml -i registered_accounts.json -o sub2api_bundle.json -v
chatgpt-register export -c config.yaml -i registered_accounts.json --format cpa -v
```

## 复用已有账号到其他 Workspace

邮箱状态文件只影响“是否还能用某个邮箱/别名注册新账号”，不影响已经注册出的账号继续加入其他 workspace。

复用已有账号时，不需要重新注册，直接使用已有 `registered_accounts.json`：

```bash
chatgpt-register join-workspace -c config.yaml -i registered_accounts.json --workspace-id <new-workspace-uuid> -t 5 -v
chatgpt-register refresh -c config.yaml -i registered_accounts.json --workspace-id <new-workspace-uuid> -t 5 -v
chatgpt-register export -c config.yaml -i registered_accounts.json -o sub2api_bundle.json -v
```

## 输出文件

常见输出：

- `registered_accounts.json`：注册成功的账号记录
- `sub2api_bundle.json` / `cpa.json` / `9router.json` 等：最终导出 JSON
- `test_run.log`：运行日志
- `data/outlook_token_state.json`：邮箱/别名状态

默认情况下，完整 `run` 的前三个文件会进入 `runs/YYYYMMDD-HHMMSS_<count>_accounts/`。


## 注意事项

- 请只使用你有权访问的邮箱、账号和 workspace。
- 并发不宜过高，过高可能触发邮箱服务或目标服务的限制。
- `login-team` 仍属于实验性流程；默认流水线使用 refresh/check 获取工作空间上下文。
- `require_team_tokens: auto` 会跟随 `workspace.re_login_enabled`。

## 致谢

感谢 [LINUX DO](https://linux.do/) 社区的交流与支持。
