# chatgpt-register-sub2api

`chatgpt-register-sub2api` 是一个用于自动化注册流程、刷新账号工作空间上下文，并导出 Sub2API 兼容 JSON 的命令行工具。

项目支持 Outlook/Gmail OAuth 邮箱池、Outlook plus alias、workspace/K12 上下文检查、并发执行和结果归档。仓库中的配置均为脱敏示例，不包含真实邮箱、密码、token、workspace ID 或运行结果。

> 请仅在你有权使用的邮箱、账号和工作空间中运行本工具，并自行确认相关服务条款和合规要求。

## 功能特性

- 支持 Outlook OAuth 邮箱池接收验证码
- 支持 Gmail OAuth/IMAP 邮箱池接收验证码
- 支持 Outlook `+数字` 别名扩展
- 支持注册、workspace 加入/申请、refresh/check、导出完整流水线
- 支持 K12/workspace 上下文识别，避免把未确认的 personal/free 上下文误导出
- 支持注册、加入、刷新等阶段并发配置
- 支持按运行时间和账号数量归档输出文件
- 支持 Sub2API 格式 JSON 导出

## 工作流程

```text
邮箱池 -> 注册账号 -> 加入/申请 workspace -> refresh/check 账号上下文 -> 导出 Sub2API JSON
```

完整运行后，默认会在 `runs/` 下生成独立目录：

```text
runs/
  20260706-093012_6_accounts/
    registered_accounts.json
    sub2api_bundle.json
    test_run.log
```

跨运行共享的邮箱状态文件保存在：

```text
data/outlook_token_state.json
```

它用于记录 Outlook 主邮箱和别名的 `used`、`failed`、`in_use` 状态，防止后续注册重复使用同一个邮箱地址。

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

## 快速开始

生成本地配置文件：

```bash
chatgpt-register init
```

编辑生成的 `config.yaml`，至少需要配置：

- 邮箱池
- 代理
- workspace ID

执行完整流水线：

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
user@outlook.com
user+1@outlook.com
user+2@outlook.com
user+3@outlook.com
user+4@outlook.com
user+5@outlook.com
```

配置示例：

```yaml
mail:
  providers:
    - type: outlook_token
      enable: true
      mode: auto
      alias_enabled: true
      alias_limit_per_mailbox: 6
      mailboxes: |
        user@outlook.com----mail_password----client_id----refresh_token

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
chatgpt-register run -c config.yaml -n 6 -t 3 --workspace-id <workspace-uuid> -v
```

结果文件位于：

```text
runs/YYYYMMDD-HHMMSS_6_accounts/sub2api_bundle.json
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
alias_limit_per_mailbox: 6
```

含义：

- `alias_enabled`：是否启用 Outlook plus alias
- `alias_limit_per_mailbox`：每个主邮箱最多使用多少个注册地址，包含主邮箱本身

验证码仍从主 Outlook 邮箱读取；注册邮箱地址则按具体别名区分。

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
| `init` | 生成默认 `config.yaml` |
| `register` | 只执行注册 |
| `join-workspace` | 对已有账号执行 workspace 加入/申请 |
| `refresh` | 刷新 token 并检查账号/workspace 上下文 |
| `login-team` | 实验性 team/workspace 重新登录流程 |
| `export` | 将已有账号记录导出为 Sub2API JSON |
| `run` | 完整流水线：register -> join -> refresh/check -> export |

示例：

```bash
chatgpt-register register -c config.yaml -n 6 -t 3 -v
chatgpt-register join-workspace -c config.yaml -i registered_accounts.json --workspace-id <workspace-uuid> -t 5 -v
chatgpt-register refresh -c config.yaml -i registered_accounts.json --workspace-id <workspace-uuid> -t 5 -v
chatgpt-register export -c config.yaml -i registered_accounts.json -o sub2api_bundle.json -v
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
- `sub2api_bundle.json`：Sub2API 兼容 bundle
- `test_run.log`：运行日志
- `data/outlook_token_state.json`：邮箱/别名状态

默认情况下，完整 `run` 的前三个文件会进入 `runs/YYYYMMDD-HHMMSS_<count>_accounts/`。

## 开源脱敏清单

公开仓库建议只保留：

- `chatgpt_register_sub2api/`
- `README.md`
- `pyproject.toml`
- `.gitignore`
- `config.example.yaml`

不要提交：

- `config.yaml` 或 `config.local.yaml`
- `data/`
- `runs/`
- `registered_accounts*.json`
- `sub2api*.json`
- `*.log`
- `test_run*.log`
- `__pycache__/`
- `.pytest_cache/`
- 虚拟环境和构建产物

发布前建议扫描敏感内容：

```bash
rg -n "outlook.com|refresh_token|access_token|id_token|session_token|workspace" .
```

占位示例可以保留；真实邮箱、OAuth token、导出 JSON、workspace ID 不应出现在公开仓库中。

## 注意事项

- 请只使用你有权访问的邮箱、账号和 workspace。
- 并发不宜过高，过高可能触发邮箱服务或目标服务的限制。
- `login-team` 仍属于实验性流程；默认流水线使用 refresh/check 获取工作空间上下文。
- `require_team_tokens: auto` 会跟随 `workspace.re_login_enabled`。

## 致谢

感谢 [LINUX DO](https://linux.do/) 社区的交流与支持。
