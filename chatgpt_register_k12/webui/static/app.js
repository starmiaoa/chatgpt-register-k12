const form = document.querySelector("#configForm");
const healthEl = document.querySelector("#health");
const statusEl = document.querySelector("#jobStatus");
const logsEl = document.querySelector("#logs");
const terminalCommandEl = document.querySelector("#terminalCommand");
const terminalOutputEl = document.querySelector("#terminalOutput");
const cancelJobEl = document.querySelector("#cancelJob");
const accountsFileEl = document.querySelector("#accountsFile");
const outputFileEl = document.querySelector("#outputFile");

let currentJobId = "";
let logSeq = 0;
let pollTimer = null;
const openedFolderJobs = new Set();

function formData() {
  const data = new FormData(form);
  const workspaceIds = String(data.get("workspace_ids") || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    config_path: data.get("config_path") || "config.yaml",
    workspace_id: workspaceIds[0] || "",
    workspace_ids: workspaceIds,
    proxy_url: data.get("proxy_url") || "",
    export_format: data.get("export_format") || "sub2api",
    count: Number(data.get("count") || 10),
    threads: Number(data.get("threads") || 2),
    alias_enabled: Boolean(data.get("alias_enabled")),
    alias_limit_per_mailbox: Number(data.get("alias_limit_per_mailbox") || 5),
    outlook_mailboxes: data.get("outlook_mailboxes") || "",
    accounts_file: accountsFileEl?.value || "",
    input_file: accountsFileEl?.value || "",
    output_file: outputFileEl?.value || ""
  };
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function renderStatus(data) {
  statusEl.textContent = JSON.stringify(data, null, 2);
  const job = data.job;
  const running = job && ["queued", "running"].includes(job.status);
  if (cancelJobEl) cancelJobEl.disabled = !running || !currentJobId;
  const artifacts = data.job?.artifacts || {};
  if (accountsFileEl && artifacts.accounts_file) accountsFileEl.value = artifacts.accounts_file;
  if (outputFileEl && artifacts.output_file) outputFileEl.value = artifacts.output_file;
}

function jobHasSuccess(job) {
  const summary = job?.summary || {};
  return ["exported", "registered", "logged_in", "joined", "refreshed"].some(
    (key) => Number(summary[key] || 0) > 0
  );
}

async function openRunFolder(job) {
  const runDir = job?.artifacts?.run_dir;
  if (!runDir || openedFolderJobs.has(job.id) || !jobHasSuccess(job)) return;
  openedFolderJobs.add(job.id);
  try {
    await api("/api/open-folder", {
      method: "POST",
      body: JSON.stringify({ path: runDir })
    });
  } catch (err) {
    logsEl.textContent += `[WARNING] 打开结果文件夹失败: ${err.message}\n`;
  }
}

async function refreshHealth() {
  try {
    const data = await api("/api/health");
    healthEl.textContent = `${data.version} · ${data.cwd}`;
  } catch (err) {
    healthEl.textContent = err.message;
  }
}

async function saveConfig() {
  const data = await api("/api/config/save", {
    method: "POST",
    body: JSON.stringify(formData())
  });
  renderStatus(data);
}

async function createJob(action) {
  logSeq = 0;
  logsEl.textContent = "";
  const payload = { ...formData(), action };
  const data = await api("/api/jobs", {
    method: "POST",
    body: JSON.stringify(payload)
  });
  currentJobId = data.job.id;
  renderStatus(data);
  startPolling();
}

async function pollJob() {
  if (!currentJobId) return;
  const data = await api(`/api/jobs/${currentJobId}`);
  renderStatus(data);
  const logs = await api(`/api/jobs/${currentJobId}/logs?after=${logSeq}`);
  for (const item of logs.logs || []) {
    logSeq = Math.max(logSeq, item.seq || 0);
    logsEl.textContent += `[${item.level}] ${item.message}\n`;
  }
  logsEl.scrollTop = logsEl.scrollHeight;
  const status = data.job?.status;
  if (["succeeded", "failed", "cancelled"].includes(status)) {
    if (status === "succeeded") await openRunFolder(data.job);
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    pollJob().catch((err) => {
      logsEl.textContent += `[ERROR] ${err.message}\n`;
    });
  }, 1200);
  pollJob().catch(() => {});
}

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      if (button.dataset.action === "save-config") await saveConfig();
      if (button.dataset.action === "preview") await createJob("preview");
    } catch (err) {
      renderStatus({ error: err.message });
    }
  });
});

document.querySelectorAll("[data-job]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await createJob(button.dataset.job);
    } catch (err) {
      renderStatus({ error: err.message });
    }
  });
});

document.querySelector("#startSelectedJob")?.addEventListener("click", async () => {
  const selected = document.querySelector('input[name="job_mode"]:checked');
  try {
    await createJob(selected?.value || "run");
  } catch (err) {
    renderStatus({ error: err.message });
  }
});

cancelJobEl?.addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    const data = await api(`/api/jobs/${currentJobId}/cancel`, {
      method: "POST",
      body: JSON.stringify({})
    });
    logsEl.textContent += data.ok
      ? "[WARNING] 已请求中断，当前网络请求结束后会停止。\n"
      : "[WARNING] 当前任务无法中断。\n";
    await pollJob();
  } catch (err) {
    logsEl.textContent += `[ERROR] ${err.message}\n`;
  }
});

document.querySelector("#refreshJobs").addEventListener("click", async () => {
  try {
    const data = await api("/api/jobs");
    renderStatus(data);
  } catch (err) {
    renderStatus({ error: err.message });
  }
});

async function runTerminalCommand() {
  const command = terminalCommandEl.value.trim();
  if (!command) return;
  terminalOutputEl.textContent += `> ${command}\n`;
  try {
    const data = await api("/api/terminal/run", {
      method: "POST",
      body: JSON.stringify({ command, timeout: 120 })
    });
    const result = data.result || {};
    if (result.output) terminalOutputEl.textContent += `${result.output}\n`;
    terminalOutputEl.textContent += `[exit ${result.returncode ?? "timeout"}]\n\n`;
  } catch (err) {
    terminalOutputEl.textContent += `[ERROR] ${err.message}\n\n`;
  }
  terminalOutputEl.scrollTop = terminalOutputEl.scrollHeight;
}

document.querySelector("#runTerminal").addEventListener("click", () => {
  runTerminalCommand();
});

terminalCommandEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runTerminalCommand();
  }
});

refreshHealth();
