/* Agent Continuity - local UI. No build step. */
(() => {
  "use strict";

  const S = {
    projects: [], project: null, tab: "overview", setup: null, settings: null,
    defs: [], defAgent: "claude", memories: [], handoffs: [], sync: null,
    conflicts: [], receipts: [], job: null, jobTimer: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function esc(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }
  function fmt(value) {
    if (!value) return "—";
    const d = new Date(value);
    return isNaN(d) ? String(value) : d.toLocaleString();
  }
  function shortSha(sha) { return sha ? String(sha).slice(0, 10) : "—"; }
  function statusBadge(status) {
    const map = { current: "current", revoked: "revoked", ready: "ready", in_progress: "ready",
                  completed: "completed", partial: "warn", failed: "failed", draft: "" };
    return `<span class="badge ${map[status] || ""}">${esc(status || "unknown")}</span>`;
  }

  async function api(path, opts = {}) {
    const init = { headers: { "Content-Type": "application/json" }, ...opts };
    if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
    const res = await fetch(path, init);
    let data;
    try { data = await res.json(); } catch { data = { ok: false, error: `HTTP ${res.status}` }; }
    if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  let toastTimer = null;
  function toast(message, bad = false) {
    const el = $("#toast");
    el.textContent = message;
    el.classList.toggle("bad", bad);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
  }

  function openModal(html) {
    $("#modal-card").innerHTML = html;
    $("#modal").classList.remove("hidden");
  }
  function closeModal() { $("#modal").classList.add("hidden"); }
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

  // ---------------------------------------------------------------- chrome

  function renderChrome() {
    const select = $("#project-select");
    select.innerHTML = S.projects.length
      ? S.projects.map(p => `<option value="${esc(p.id)}" ${S.project && p.id === S.project.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")
      : `<option value="">还没有项目</option>`;
    if (S.setup && S.setup.checks) {
      const bad = S.setup.checks.filter(c => !c.ok).length;
      const name = S.setup.device ? S.setup.device.name : "this device";
      $("#device-label").textContent = `${name}${bad ? ` · ${bad} 项检查待处理` : " · 环境就绪"}`;
    } else {
      $("#device-label").textContent = "";
    }
    $("#home-path").textContent = S.setup && S.setup.home ? S.setup.home : "~/.agent-continuity";
    const pill = $("#sync-pill");
    if (!S.project) { pill.textContent = "local"; pill.className = "pill"; return; }
    const dirty = S.sync && S.sync.dirty ? S.sync.dirty.length : 0;
    const remote = S.project.remote ? "git" : "local";
    pill.textContent = `${remote}${dirty ? ` · ${dirty} 未提交` : ""}`;
    pill.className = `pill ${dirty ? "warn" : "ok"}`;
  }

  async function boot() {
    try {
      const [setup, projects] = await Promise.all([api("/api/setup"), api("/api/projects")]);
      S.setup = setup;
      S.settings = setup.settings;
      const versionEl = document.getElementById("app-version");
      if (versionEl && setup.version) versionEl.textContent = "v" + setup.version;
      S.projects = projects.projects;
      const saved = localStorage.getItem("ac-project");
      S.project = S.projects.find(p => p.id === saved) || S.projects[0] || null;
      renderChrome();
      await render();
    } catch (e) {
      $("#content").innerHTML = `<div class="empty"><h3>无法连接本地服务</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function refreshProject() {
    if (!S.project) return;
    try {
      const data = await api(`/api/projects/${S.project.id}`);
      S.project = data.project;
      S.projects = S.projects.map(p => p.id === S.project.id ? S.project : p);
      localStorage.setItem("ac-project", S.project.id);
      renderChrome();
    } catch (e) { toast(e.message, true); }
  }

  async function selectProject(pid) {
    if (!pid) return;
    S.project = S.projects.find(p => p.id === pid) || null;
    S.tab = "overview";
    if (S.jobTimer) clearInterval(S.jobTimer);
    S.job = null;
    localStorage.setItem("ac-project", pid);
    await refreshProject();
    await render();
  }

  async function setTab(tab) {
    S.tab = tab;
    $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    await render();
  }

  // ---------------------------------------------------------------- render

  async function render() {
    const c = $("#content");
    if (!S.project) { c.innerHTML = onboarding(); return; }
    c.innerHTML = `<div class="empty">加载中…</div>`;
    try {
      if (S.tab === "overview") await renderOverview();
      else if (S.tab === "definitions") await renderDefinitions();
      else if (S.tab === "memory") await renderMemory();
      else if (S.tab === "handoff") await renderHandoff();
      else if (S.tab === "devices") await renderDevices();
      else if (S.tab === "settings") await renderSettings();
      renderChrome();
    } catch (e) {
      c.innerHTML = `<div class="card"><h2>加载失败</h2><p class="muted">${esc(e.message)}</p><button class="btn" onclick="AC.render()">重试</button></div>`;
    }
  }

  function onboarding() {
    return `<div class="empty" style="max-width:680px;margin:60px auto">
      <h3>从一个真实项目开始</h3>
      <p>Agent Continuity 把项目定义、记忆、交接和同步放在本地。先登记一个项目目录，它不会被复制或上传。</p>
      <div class="row" style="justify-content:center">
        <button class="btn primary" onclick="AC.openNewProject()">新建项目</button>
        <button class="btn" onclick="AC.openJoinProject()">加入已有项目</button>
      </div>
    </div>`;
  }

  function pageHead(title, sub) {
    return `<div class="page-head"><div><h1>${esc(title)}</h1><div class="sub">${esc(sub || "")}</div></div>
      <div class="row gap"><button class="btn small" onclick="AC.refreshAll()">刷新</button></div></div>`;
  }

  async function renderOverview() {
    await refreshProject();
    const p = S.project;
    let receipts = [];
    try { receipts = (await api(`/api/projects/${p.id}/receipts`)).receipts.slice(-6).reverse(); } catch {}
    const checks = (S.setup && S.setup.checks) || [];
    const bad = checks.filter(c => !c.ok);
    const latest = p.handoff_latest;
    const activeDefs = p.definitions || [];
    $("#content").innerHTML = `${pageHead(p.name, p.path || "尚未设置本机路径")}
      <div class="grid two">
        <div class="card">
          <div class="card-head"><h2>当前状态</h2><span class="pill ${p.remote ? "ok" : ""}">${p.remote ? "Git 已连接" : "仅本机"}</span></div>
          <div class="grid three">
            <div><div class="stat">${activeDefs.length}</div><div class="stat-label">Agent 定义</div></div>
            <div><div class="stat">${p.memory_current}</div><div class="stat-label">当前记忆</div></div>
            <div><div class="stat">${p.device_count}</div><div class="stat-label">登记设备</div></div>
          </div>
          <div class="hr"></div>
          <dl class="kv">
            <dt>项目 ID</dt><dd><code>${esc(p.id)}</code></dd>
            <dt>数据目录</dt><dd><code>${esc(p.data_dir)}</code></dd>
            <dt>最近提交</dt><dd>${p.last_commit ? `<code>${shortSha(p.last_commit.sha)}</code> ${esc(p.last_commit.subject)}` : "—"}</dd>
          </dl>
        </div>
        <div class="card">
          <div class="card-head"><h2>下一步</h2><button class="btn small primary" onclick="AC.setTab('handoff')">准备接续</button></div>
          ${latest ? `<div class="stack">
            <div class="row between"><strong>${esc(latest.goal || "未命名交接")}</strong>${statusBadge(latest.status)}</div>
            <div class="muted small">下一步：${esc(latest.next || "—")}</div>
            <div class="muted small">${latest.dirty_files && latest.dirty_files.length ? `${latest.dirty_files.length} 个未提交文件` : "工作树干净"} · ${fmt(latest.created_at)}</div>
            <div class="row gap"><button class="btn small primary" onclick="AC.continueHandoff('${latest.id}','claude')">用 Claude Code 继续</button>
            <button class="btn small" onclick="AC.continueHandoff('${latest.id}','codex')">用 Codex 继续</button></div>
          </div>` : `<div class="empty" style="padding:22px"><h3>还没有交接</h3><p>把当前目标、已完成工作和下一步写下来，另一个 Agent 才能接着做。</p><button class="btn primary" onclick="AC.setTab('handoff')">创建交接</button></div>`}
        </div>
      </div>
      <div class="grid two" style="margin-top:16px">
        <div class="card">
          <div class="card-head"><h2>环境检查</h2><span class="pill ${bad.length ? "warn" : "ok"}">${bad.length ? `${bad.length} 项待处理` : "就绪"}</span></div>
          ${checks.map(c => `<div class="check ${c.ok ? "ok" : "bad"}"><span class="dot"></span><div><div>${esc(c.label)}</div><div class="muted small">${esc(c.detail)}</div>${!c.ok && c.fix ? `<div class="fix">${esc(c.fix)}</div>` : ""}</div></div>`).join("")}
        </div>
        <div class="card">
          <div class="card-head"><h2>最近证据</h2><span class="muted small">saved / delivered / read / used</span></div>
          ${receipts.length ? receipts.map(r => `<div class="check"><span class="dot" style="background:${r.layer === "used" ? "var(--accent)" : r.layer === "read" ? "#7b8f88" : "#c9c9c2"}"></span><div><div><strong>${esc(r.layer)}</strong> · ${esc(r.record_type)} <code>${esc(r.record_id)}</code></div><div class="muted small">${fmt(r.at)}${r.session_id ? ` · session ${esc(r.session_id)}` : ""}${r.detail ? ` · ${esc(r.detail)}` : ""}</div></div></div>`).join("") : `<div class="empty" style="padding:20px">还没有证据记录。</div>`}
        </div>
      </div>`;
  }

  // ------------------------------------------------------------- definitions

  async function renderDefinitions() {
    const data = await api(`/api/projects/${S.project.id}/definitions`);
    S.defs = data.definitions || [];
    // Keep a supported user selection even when this host has no revisions yet.
    if (!["claude", "codex"].includes(S.defAgent)) S.defAgent = "claude";
    const current = S.defs.find(d => d.agent === S.defAgent) || { agent: S.defAgent, revisions: [], active_revision: null, applied: null };
    $("#content").innerHTML = `${pageHead("Agent 版本", "定义是版本化的；应用只改受管区段，可恢复。")}
      <div class="grid side">
        <div class="card">
          <div class="card-head"><h2>版本</h2><select style="width:auto" onchange="AC.setDefAgent(this.value)">${["claude", "codex"].map(a => `<option ${a === S.defAgent ? "selected" : ""}>${a}</option>`).join("")}</select></div>
          ${current.revisions && current.revisions.length ? `<div class="list">${current.revisions.slice().reverse().map(r => `<div class="list-item ${r.rev === current.active_revision ? "selected" : ""}" onclick="AC.showDefinition('${current.agent}','${r.rev}')"><div><div class="title">${esc(r.name || r.rev)} ${r.rev === current.active_revision ? '<span class="badge current">active</span>' : ""}</div><div class="meta">${esc(r.rev)} · ${fmt(r.created_at)} · ${esc(r.source || "")}</div></div><span class="muted small">${shortSha(r.sha256)}</span></div>`).join("")}</div>` : `<div class="empty" style="padding:20px"><h3>还没有定义</h3><p>在右边写第一版。</p></div>`}
          ${current.applied ? `<div class="hr"></div><div class="muted small">已应用到 <code>${esc(current.applied.target)}</code> · ${fmt(current.applied.applied_at)}</div><button class="btn small" style="margin-top:8px" onclick="AC.restoreDefinition('${current.agent}')">恢复应用前内容</button>` : ""}
        </div>
        <div class="card">
          <h2>新建版本</h2>
          <div class="row gap" style="margin-bottom:10px"><input type="file" id="def-file" accept=".md,.txt" style="max-width:260px"><button class="btn small" onclick="AC.importDefinitionFile()">导入文件</button></div>
          <div class="field"><span>名称</span><input id="def-name" placeholder="例如：exporter v3"></div>
          <div class="field"><span>内容（Markdown）</span><textarea id="def-content" style="min-height:240px" placeholder="角色、约束、允许的工具、输出要求…"></textarea></div>
          <div class="row between"><span class="muted small">保存后会成为一个新 revision；激活和应用是两步。</span><button class="btn primary" onclick="AC.saveDefinition()">保存新版本</button></div>
        </div>
      </div>`;
  }

  async function showDefinition(agent, rev) {
    try {
      const data = await api(`/api/projects/${S.project.id}/definitions/${encodeURIComponent(agent)}/${encodeURIComponent(rev)}`);
      const d = data.definition;
      const defs = S.defs.find(x => x.agent === agent) || {};
      let diff = "";
      if (defs.active_revision && defs.active_revision !== rev) {
        const dd = await api(`/api/projects/${S.project.id}/definitions/${encodeURIComponent(agent)}/diff?a=${encodeURIComponent(defs.active_revision)}&b=${encodeURIComponent(rev)}`);
        diff = dd.diff;
      }
      openModal(`<div class="row between"><h2>${esc(d.name || rev)}</h2><span class="badge ${d.active ? "current" : ""}">${d.active ? "active" : "revision"}</span></div>
        <div class="muted small">${esc(agent)} / ${esc(rev)} · ${fmt(d.created_at)} · sha ${shortSha(d.sha256)}</div>
        <div class="hr"></div>
        <pre>${esc(d.content)}</pre>
        ${diff ? `<h3 style="margin-top:16px">与 active 的差异</h3><pre>${diff.split("\n").map(line => `<span class="${line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : ""}">${esc(line)}</span>`).join("\n")}</pre>` : ""}
        <div class="row wrap" style="margin-top:14px">
          <button class="btn" onclick="AC.activateDefinition('${agent}','${rev}')">设为 active</button>
          <select id="apply-target" style="width:auto"><option>CLAUDE.md</option><option>AGENTS.md</option></select>
          <button class="btn primary" onclick="AC.applyDefinition('${agent}','${rev}')">应用到项目</button>
          <span class="spacer"></span><button class="btn ghost" onclick="AC.closeModal()">关闭</button>
        </div>`);
    } catch (e) { toast(e.message, true); }
  }

  async function importDefinitionFile() {
    const file = $("#def-file") && $("#def-file").files && $("#def-file").files[0];
    if (!file) return toast("先选择一个 Markdown/文本文件", true);
    const text = await file.text();
    if ($("#def-content")) $("#def-content").value = text;
    if ($("#def-name") && !$("#def-name").value) $("#def-name").value = file.name.replace(/\.[^.]+$/, "");
    toast(`已导入 ${file.name}；保存后会成为新 revision`);
  }

  async function saveDefinition() {
    const name = $("#def-name").value.trim();
    const content = $("#def-content").value;
    if (!content.trim()) return toast("定义内容不能为空", true);
    try {
      await api(`/api/projects/${S.project.id}/definitions`, { method: "POST", body: { agent: S.defAgent, name, content } });
      toast("已保存新版本");
      await renderDefinitions();
    } catch (e) { toast(e.message, true); }
  }

  async function activateDefinition(agent, rev) {
    try {
      await api(`/api/projects/${S.project.id}/definitions/${encodeURIComponent(agent)}/${encodeURIComponent(rev)}/activate`, { method: "POST" });
      toast("已设为 active"); closeModal(); await renderDefinitions();
    } catch (e) { toast(e.message, true); }
  }

  async function applyDefinition(agent, rev) {
    const target = $("#apply-target") ? $("#apply-target").value : "CLAUDE.md";
    try {
      await api(`/api/projects/${S.project.id}/definitions/${encodeURIComponent(agent)}/${encodeURIComponent(rev)}/apply`, { method: "POST", body: { target } });
      toast(`已应用到 ${target}`); closeModal(); await renderDefinitions();
    } catch (e) { toast(e.message, true); }
  }

  async function restoreDefinition(agent) {
    if (!confirm("恢复应用前的文件内容？只移除/还原受管区段。")) return;
    try {
      await api(`/api/projects/${S.project.id}/definitions/${encodeURIComponent(agent)}/restore`, { method: "POST" });
      toast("已恢复"); await renderDefinitions();
    } catch (e) { toast(e.message, true); }
  }

  // ----------------------------------------------------------------- memory

  async function renderMemory() {
    await loadMemories(false);
    $("#content").innerHTML = `${pageHead("记忆", "当前事实、历史、来源与撤销状态分开记录；配置回滚不会复活已撤销记忆。")}
      <div class="card" style="margin-bottom:16px">
        <div class="form-grid">
          <div class="field wide"><span>标题</span><input id="mem-title" placeholder="例如：当前导出 timeout"></div>
          <div class="field wide"><span>内容</span><textarea id="mem-text" placeholder="写一条会被另一个 Agent 使用的事实"></textarea></div>
          <div class="field"><span>范围</span><select id="mem-scope"><option value="project">项目</option><option value="agent">Agent</option><option value="personal">个人</option></select></div>
          <div class="field"><span>来源</span><input id="mem-source" value="web" placeholder="user / web / import"></div>
        </div>
        <div class="row between"><span class="muted small">保存后立即可搜索；撤销保留历史。</span><button class="btn primary" onclick="AC.addMemory()">添加记忆</button></div>
      </div>
      <div class="card flush">
        <div class="toolbar" style="padding:12px 16px;margin:0;border-bottom:1px solid var(--line)">
          <input id="mem-q" placeholder="搜索记忆…" style="max-width:280px" oninput="AC.searchMemories()">
          <select id="mem-status" style="width:auto" onchange="AC.searchMemories()"><option value="">全部状态</option><option value="current">current</option><option value="revoked">revoked</option></select>
          <span class="spacer"></span><span class="muted small" id="mem-count">${S.memories.length} 条</span>
        </div>
        <div id="mem-list">${memoryListHtml()}</div>
      </div>`;
  }

  async function loadMemories(rerender = true) {
    const q = $("#mem-q") ? $("#mem-q").value : "";
    const status = $("#mem-status") ? $("#mem-status").value : "";
    const data = await api(`/api/projects/${S.project.id}/memories?q=${encodeURIComponent(q)}&status=${encodeURIComponent(status)}`);
    S.memories = data.memories || [];
    if (rerender) {
      const list = $("#mem-list");
      if (list) list.innerHTML = memoryListHtml();
      const count = $("#mem-count"); if (count) count.textContent = `${S.memories.length} 条`;
    }
  }

  function memoryListHtml() {
    if (!S.memories.length) return `<div class="empty" style="margin:16px">没有匹配的记忆。</div>`;
    return `<div class="list">${S.memories.map(m => `<div class="list-item">
      <div style="min-width:0">
        <div class="title">${esc(m.title)} ${statusBadge(m.status)} <span class="muted small">${esc(m.scope)}</span></div>
        <div style="margin:4px 0 6px;white-space:pre-wrap">${esc(m.text)}</div>
        <div class="meta">rev ${esc(m.current_revision)} · ${fmt(m.updated_at)} · ${esc((m.revisions && m.revisions.length) || 0)} 个历史版本</div>
      </div>
      <div class="row gap" style="flex:none">
        <button class="btn small" onclick="AC.editMemory('${m.id}')">编辑</button>
        ${m.status === "revoked" ? `<button class="btn small" onclick="AC.setMemoryStatus('${m.id}','current')">恢复</button>` : `<button class="btn small danger" onclick="AC.setMemoryStatus('${m.id}','revoked')">停用</button>`}
        <button class="btn small ghost" onclick="AC.memoryHistory('${m.id}')">历史</button>
      </div>
    </div>`).join("")}</div>`;
  }

  async function addMemory() {
    const title = $("#mem-title").value.trim();
    const text = $("#mem-text").value;
    if (!text.trim()) return toast("内容不能为空", true);
    try {
      await api(`/api/projects/${S.project.id}/memories`, { method: "POST", body: { title, text, scope: $("#mem-scope").value, source: $("#mem-source").value } });
      toast("已添加"); await renderMemory();
    } catch (e) { toast(e.message, true); }
  }

  async function searchMemories() { try { await loadMemories(true); } catch (e) { toast(e.message, true); } }

  async function editMemory(mid) {
    try {
      const data = await api(`/api/projects/${S.project.id}/memories/${mid}`);
      const m = data.memory;
      openModal(`<h2>编辑记忆</h2><div class="muted small">${esc(m.id)} · 当前 ${esc(m.current_revision)}</div>
        <div class="field" style="margin-top:14px"><span>标题</span><input id="edit-mem-title" value="${esc(m.title)}"></div>
        <div class="field"><span>内容</span><textarea id="edit-mem-text" style="min-height:180px">${esc(m.text)}</textarea></div>
        <div class="modal-actions"><button class="btn ghost" onclick="AC.closeModal()">取消</button><button class="btn primary" onclick="AC.saveMemory('${mid}')">保存新修订</button></div>`);
    } catch (e) { toast(e.message, true); }
  }

  async function saveMemory(mid) {
    try {
      await api(`/api/projects/${S.project.id}/memories/${mid}`, { method: "PATCH", body: { title: $("#edit-mem-title").value, text: $("#edit-mem-text").value } });
      toast("已保存新修订"); closeModal(); await renderMemory();
    } catch (e) { toast(e.message, true); }
  }

  async function setMemoryStatus(mid, status) {
    try {
      await api(`/api/projects/${S.project.id}/memories/${mid}`, { method: "PATCH", body: { status } });
      toast(status === "revoked" ? "已停用（历史保留）" : "已显式恢复"); await renderMemory();
    } catch (e) { toast(e.message, true); }
  }

  async function memoryHistory(mid) {
    try {
      const data = await api(`/api/projects/${S.project.id}/memories/${mid}`);
      const m = data.memory;
      openModal(`<h2>历史 · ${esc(m.title)}</h2><div class="muted small">${esc(m.id)} · ${statusBadge(m.status)}</div>
        <div class="hr"></div>${m.revisions.slice().reverse().map(r => `<div class="card" style="margin-bottom:10px;box-shadow:none"><div class="row between"><strong>${esc(r.rev)}</strong><span class="muted small">${fmt(r.created_at)} · ${esc(r.source || "")}</span></div><pre style="margin:8px 0 0">${esc(r.text)}</pre></div>`).join("")}
        <div class="modal-actions"><button class="btn" onclick="AC.closeModal()">关闭</button></div>`);
    } catch (e) { toast(e.message, true); }
  }

  // ---------------------------------------------------------------- handoff

  async function renderHandoff() {
    const data = await api(`/api/projects/${S.project.id}/handoffs`);
    S.handoffs = data.handoffs || [];
    $("#content").innerHTML = `${pageHead("接续", "把目标、已完成、下一步和证据写清楚；从网页发起真实 CLI 任务。")}
      <div class="card" style="margin-bottom:16px">
        <h2>准备交接</h2>
        <div class="form-grid">
          <div class="field wide"><span>当前目标</span><input id="h-goal" placeholder="这次要继续完成什么"></div>
          <div class="field"><span>已完成</span><textarea id="h-done" placeholder="已经做完并验证过的事"></textarea></div>
          <div class="field"><span>下一步</span><textarea id="h-next" placeholder="接手 Agent 应该先做什么"></textarea></div>
          <div class="field wide"><span>证据 / 约束</span><textarea id="h-evidence" placeholder="测试结果、已知限制、不要做什么…"></textarea></div>
          <div class="field"><span>默认宿主</span><select id="h-agent"><option value="claude">Claude Code</option><option value="codex">Codex</option></select></div>
        </div>
        <div class="row between"><label class="row gap muted small" style="font-weight:500"><input type="checkbox" id="h-sync" style="width:auto"> 创建后推送到 remote</label><button class="btn primary" onclick="AC.createHandoff()">创建交接</button></div>
      </div>
      <div class="card flush">
        <div class="card-head" style="padding:14px 16px;margin:0;border-bottom:1px solid var(--line)"><h2>交接记录</h2><span class="muted small">${S.handoffs.length} 条</span></div>
        ${S.handoffs.length ? `<div class="list">${S.handoffs.map(h => `<div class="list-item">
          <div style="min-width:0"><div class="title">${esc(h.goal || "未命名交接")} ${statusBadge(h.status)}</div>
            <div class="muted small" style="margin-top:3px">下一步：${esc(h.next || "—")}</div>
            <div class="meta">${esc(h.id)} · ${fmt(h.created_at)} · base ${shortSha(h.base_commit)} · ${h.dirty_files && h.dirty_files.length ? `${h.dirty_files.length} 个未提交` : "干净"}</div>
          </div>
          <div class="row gap" style="flex:none">
            <button class="btn small primary" onclick="AC.continueHandoff('${h.id}','claude')">Claude Code</button>
            <button class="btn small" onclick="AC.continueHandoff('${h.id}','codex')">Codex</button>
            <button class="btn small ghost" onclick="AC.handoffDetail('${h.id}')">详情</button>
          </div></div>`).join("")}</div>` : `<div class="empty" style="margin:16px"><h3>还没有交接</h3><p>填写上面的表单创建第一条。</p></div>`}
      </div>
      <div id="job-panel"></div>`;
    if (S.job) renderJob();
  }

  async function createHandoff() {
    const body = { goal: $("#h-goal").value, done: $("#h-done").value, next: $("#h-next").value, evidence: $("#h-evidence").value, agent: $("#h-agent").value };
    if (!body.goal.trim() || !body.next.trim()) return toast("目标和下一步不能为空", true);
    try {
      await api(`/api/projects/${S.project.id}/handoffs`, { method: "POST", body });
      if ($("#h-sync") && $("#h-sync").checked) {
        if (!S.project.remote) toast("交接已创建；还没有 remote，未推送", true);
        else {
          try { await api(`/api/projects/${S.project.id}/sync/push`, { method: "POST", body: {} }); toast("交接已创建并推送"); }
          catch (e) { toast(`交接已创建，但推送失败：${e.message}`, true); }
        }
      } else toast("交接已创建");
      await renderHandoff();
    } catch (e) { toast(e.message, true); }
  }

  async function handoffDetail(hid) {
    try {
      const [data, receiptData] = await Promise.all([
        api(`/api/projects/${S.project.id}/handoffs/${hid}`),
        api(`/api/projects/${S.project.id}/receipts?record_id=${encodeURIComponent(hid)}`).catch(() => ({ receipts: [] })),
      ]);
      const h = data.handoff;
      const receipts = receiptData.receipts || [];
      openModal(`<h2>${esc(h.goal || "交接")}</h2><div class="row gap">${statusBadge(h.status)}<span class="muted small">${esc(h.id)}</span></div>
        <div class="hr"></div><dl class="kv">
          <dt>已完成</dt><dd style="white-space:pre-wrap">${esc(h.done || "—")}</dd>
          <dt>下一步</dt><dd style="white-space:pre-wrap">${esc(h.next || "—")}</dd>
          <dt>证据 / 约束</dt><dd style="white-space:pre-wrap">${esc(h.evidence || "—")}</dd>
          <dt>base commit</dt><dd><code>${esc(h.base_commit || "—")}</code></dd>
          <dt>未提交文件</dt><dd>${h.dirty_files && h.dirty_files.length ? `<pre>${esc(h.dirty_files.join("\n"))}</pre>` : "无"}</dd>
          <dt>记忆快照</dt><dd><code>${esc(h.data_commit || "—")}</code></dd>
        </dl>
        ${h.jobs && h.jobs.length ? `<h3 style="margin-top:16px">最近执行</h3>${h.jobs.slice(0, 3).map(j => `<div class="card" style="box-shadow:none;margin-bottom:10px">
          <div class="row between"><strong>${esc(j.host)}</strong><span class="badge ${j.status === "completed" ? "completed" : j.status === "failed" ? "failed" : "ready"}">${esc(j.status)}</span></div>
          <div class="muted small">${j.session_id ? `session <code>${esc(j.session_id)}</code> · ` : ""}${j.duration_ms ? `${j.duration_ms} ms` : ""}${j.markers ? ` · read receipt ${j.markers.read ? "yes" : "unknown"} · done reported ${j.markers.done ? "yes" : "unknown"}` : ""}</div>
          ${j.session_dir ? `<div class="muted small">工作区 <code>${esc(j.session_dir)}</code> <button class="btn small ghost" onclick="AC.openWorkspace()">打开</button> <button class="btn small ghost" onclick="AC.syncWorkspace()">检查并回传</button></div>` : ""}
          ${j.result ? `<pre style="margin-top:8px">${esc(j.result)}</pre>` : ""}
          ${j.synced_files && j.synced_files.length ? `<div class="badge ok" style="margin-top:8px">已复制 ${j.synced_files.length} 个文件：${esc(j.synced_files.join(", "))}</div>` : ""}
          ${j.updated_files && j.updated_files.length ? `<div class="badge ok" style="margin-top:8px">已更新此前回传的文件：${esc(j.updated_files.join(", "))}</div>` : ""}
          ${j.kept_in_session && j.kept_in_session.length ? `<div class="badge warn" style="margin-top:8px">冲突未覆盖：${esc(j.kept_in_session.join(", "))}</div>` : ""}
          ${j.sync_errors && j.sync_errors.length ? `<div class="badge bad" style="margin-top:8px">回传错误：${esc(j.sync_errors.join("; "))}</div>` : ""}
          ${j.error ? `<div class="badge bad" style="margin-top:8px">${esc(j.error)}</div>` : ""}
          ${j.warnings && j.warnings.length ? `<div class="badge warn" style="margin-top:8px">${esc(j.warnings.join("; "))}</div>` : ""}
        </div>`).join("")}` : ""}
        ${receipts.length ? `<h3 style="margin-top:16px">证据层</h3><div class="list" style="border:1px solid var(--line);border-radius:8px">${receipts.map(r => `<div class="list-item" style="padding:8px 12px"><div><strong>${esc(r.layer)}</strong> <span class="muted small">rev ${esc(r.revision)}</span><div class="muted small">${fmt(r.at)}${r.session_id ? ` · session ${esc(r.session_id)}` : ""}${r.detail ? ` · ${esc(r.detail)}` : ""}</div></div></div>`).join("")}</div>` : `<p class="muted small" style="margin-top:12px">还没有 read/used 证据；运行一次接续后会出现。</p>`}
        <div class="modal-actions"><button class="btn ghost" onclick="AC.closeModal()">关闭</button></div>`);
    } catch (e) { toast(e.message, true); }
  }

  async function openWorkspace() {
    if (!S.project) return toast("先选择一个项目", true);
    try {
      const data = await api(`/api/projects/${S.project.id}/session/open`, { method: "POST", body: {} });
      toast(`已打开 ${data.path}`);
    } catch (e) { toast(e.message, true); }
  }

  async function syncWorkspace() {
    if (!S.project) return toast("先选择一个项目", true);
    try {
      const data = await api(`/api/projects/${S.project.id}/session/sync`, { method: "POST", body: {} });
      const s = data.sync;
      const updated = s.updated || [];
      const bits = [];
      if (s.copied.length - updated.length > 0) bits.push(`复制 ${s.copied.length - updated.length} 个`);
      if (updated.length) bits.push(`更新 ${updated.length} 个`);
      if (s.conflicts.length) bits.push(`冲突 ${s.conflicts.length} 个`);
      if (s.errors.length) bits.push(`错误 ${s.errors.length} 个`);
      toast(bits.length ? `回传完成：${bits.join("，")}` : "没有需要回传的新文件", !!s.errors.length);
      openModal(`<h2>工作区回传</h2>
        <div class="muted small"><code>${esc(s.session_dir)}</code> → <code>${esc(s.project_path)}</code></div>
        <div class="hr"></div>
        ${s.copied.length - updated.length > 0 ? `<h3>已新增</h3><pre>${esc(s.copied.filter(f => !updated.includes(f)).join("\n"))}</pre>` : ""}
        ${updated.length ? `<h3>已更新（项目里仍是上次回传的内容）</h3><pre>${esc(updated.join("\n"))}</pre>` : ""}
        ${s.identical.length ? `<h3>内容相同（跳过）</h3><pre>${esc(s.identical.join("\n"))}</pre>` : ""}
        ${s.conflicts.length ? `<h3>冲突，未覆盖</h3><pre>${esc(s.conflicts.join("\n"))}</pre>` : ""}
        ${s.errors.length ? `<h3>错误</h3><pre>${esc(s.errors.join("\n"))}</pre>` : ""}
        ${!s.copied.length && !s.conflicts.length && !s.errors.length ? `<p class="muted">工作区里没有新文件。</p>` : ""}
        <div class="modal-actions"><button class="btn ghost" onclick="AC.closeModal()">关闭</button></div>`);
    } catch (e) { toast(e.message, true); }
  }

  async function continueHandoff(hid, host) {
    try {
      const data = await api(`/api/projects/${S.project.id}/handoffs/${hid}/continue`, { method: "POST", body: { host } });
      S.job = { id: data.job_id, status: "running", host, handoff: hid };
      toast(`已启动 ${host} 任务`);
      if (S.jobTimer) clearInterval(S.jobTimer);
      S.jobTimer = setInterval(pollJob, 1200);
      if (S.tab !== "handoff") await setTab("handoff"); else renderJob();
    } catch (e) { toast(e.message, true); }
  }

  async function pollJob() {
    if (!S.job) return;
    try {
      const data = await api(`/api/jobs/${S.job.id}`);
      S.job = data.job;
      renderJob();
      if (S.job.status !== "running") {
        clearInterval(S.jobTimer); S.jobTimer = null;
        await refreshProject();
        if (S.tab === "handoff") await renderHandoff();
      }
    } catch (e) { clearInterval(S.jobTimer); S.jobTimer = null; toast(e.message, true); }
  }

  function renderJob() {
    const panel = $("#job-panel");
    if (!panel || !S.job) return;
    const j = S.job;
    const r = j.result || {};
    const status = j.status || "running";
    panel.innerHTML = `<div class="card" style="margin-top:16px">
      <div class="card-head"><h2>执行状态 · ${esc(j.host || "")}</h2><span class="pill ${status === "completed" ? "ok" : status === "failed" ? "bad" : "warn"}">${esc(status)}</span></div>
      ${r.session_id ? `<div class="muted small">session <code>${esc(r.session_id)}</code> · ${r.duration_ms ? `${r.duration_ms} ms` : ""}</div>` : ""}
      ${r.markers ? `<div class="row wrap" style="margin-top:8px">
        <span class="badge ${r.markers.read ? "ok" : "warn"}">read receipt ${r.markers.read ? "yes" : "unknown"}</span>
        <span class="badge ${r.markers.done ? "ok" : "warn"}">done reported ${r.markers.done ? "yes" : "unknown"}</span>
        ${r.markers.blocked ? `<span class="badge bad">blocked</span>` : ""}
      </div>` : ""}
      ${j.error ? `<p class="badge bad" style="margin-top:10px">${esc(j.error)}</p>` : ""}
      ${r.warnings && r.warnings.length ? `<p class="badge warn" style="margin-top:10px">${esc(r.warnings.join("; "))}</p>` : ""}
      ${r.result ? `<h3 style="margin-top:14px">结果</h3><pre>${esc(r.result)}</pre>` : ""}
      ${r.session_dir ? `<div class="hr"></div><h3>接续工作区</h3>
        <p class="muted small">这个 session 在本项目自己的工作区里运行。在 Claude Desktop 打开同一目录，就能找到它并继续对话；恢复会话后新增的文件可以用“检查并回传”。</p>
        <div class="row wrap gap" style="align-items:center"><code class="small">${esc(r.session_dir)}</code><button class="btn small" onclick="AC.openWorkspace()">打开文件夹</button><button class="btn small" onclick="AC.syncWorkspace()">检查并回传</button></div>` : ""}
      ${r.synced_files && r.synced_files.length ? `<div class="badge ok" style="margin-top:10px">已复制到项目 ${r.synced_files.length} 个文件：${esc(r.synced_files.join(", "))}</div>` : ""}
      ${r.kept_in_session && r.kept_in_session.length ? `<div class="badge warn" style="margin-top:10px">冲突未覆盖（项目里已有不同内容）：${esc(r.kept_in_session.join(", "))}</div>` : ""}
      ${r.sync_errors && r.sync_errors.length ? `<div class="badge bad" style="margin-top:10px">回传错误：${esc(r.sync_errors.join("; "))}</div>` : ""}
      ${r.markers ? `<div class="muted small" style="margin-top:10px">read 标记：${r.marker_read ? "模型打印了 AC_READ" : "未打印"} · 实际工具读取：${r.tool_read ? "检测到" : "未检测到"}（两者不是同一层证据）</div>` : ""}
      ${r.raw_log ? `<details style="margin-top:10px"><summary class="muted small">原始日志</summary><pre>${esc(r.raw_log)}</pre></details>` : ""}
      ${status === "running" ? `<div class="muted small" style="margin-top:10px">任务在后台运行；可以离开这个标签页，稍后回来刷新。</div>` : ""}
    </div>`;
  }

  // ---------------------------------------------------------------- devices

  async function renderDevices() {
    const [syncData, projData] = await Promise.all([
      api(`/api/projects/${S.project.id}/sync`),
      api(`/api/projects/${S.project.id}`),
    ]);
    S.sync = syncData.sync;
    S.project = projData.project;
    const conflicts = S.sync.conflicts || [];
    $("#content").innerHTML = `${pageHead("设备与同步", "每台设备保留本地副本；同步使用普通 Git，不复制活跃数据库或凭据。")}
      <div class="grid two">
        <div class="card">
          <h2>本机路径</h2>
          <div class="field"><span>当前设备上的项目目录</span><input id="device-path" value="${esc(S.project.path || "")}"></div>
          <button class="btn" onclick="AC.saveDevicePath()">保存路径</button>
          <div class="hr"></div>
          <h3>登记设备</h3>
          ${Object.entries(S.project.devices || {}).map(([id, path]) => `<div class="check"><span class="dot ${id === (S.setup && S.setup.device && S.setup.device.id) ? "ok" : ""}"></span><div><div>${esc(id)} ${id === (S.setup && S.setup.device && S.setup.device.id) ? '<span class="badge current">this device</span>' : ""}</div><div class="muted small"><code>${esc(path)}</code></div></div></div>`).join("")}
        </div>
        <div class="card">
          <h2>Git 同步</h2>
          <div class="field"><span>数据仓库 remote</span><input id="sync-remote" value="${esc(S.sync.remote || "")}" placeholder="例如 git@github.com:you/agent-continuity-data.git"></div>
          <div class="row wrap gap"><button class="btn" onclick="AC.setRemote()">保存 remote</button><button class="btn" onclick="AC.syncAction('commit')">提交本地</button><button class="btn" onclick="AC.syncAction('push')">推送</button><button class="btn primary" onclick="AC.syncAction('pull')">拉取</button></div>
          <div class="hr"></div>
          <dl class="kv">
            <dt>状态</dt><dd>${S.sync.dirty && S.sync.dirty.length ? `${S.sync.dirty.length} 个未提交文件` : "干净"}</dd>
            <dt>ahead / behind</dt><dd>${S.sync.ahead} / ${S.sync.behind}</dd>
            <dt>最近提交</dt><dd>${S.sync.last_commit ? `<code>${esc(S.sync.last_commit.split("|")[0].slice(0, 10))}</code> ${esc((S.sync.last_commit.split("|")[1] || ""))}` : "—"}</dd>
          </dl>
        </div>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="card-head"><h2>冲突</h2><span class="pill ${conflicts.length ? "warn" : "ok"}">${conflicts.length ? `${conflicts.length} 个待处理` : "无冲突"}</span></div>
        ${conflicts.length ? conflicts.map(c => `<div class="card" style="box-shadow:none;margin-bottom:12px">
          <div class="row between"><strong><code>${esc(c.path)}</code></strong><div class="row gap"><button class="btn small" onclick="AC.resolveConflict(decodeURIComponent('${encodeURIComponent(c.path)}'),'local')">保留本机</button><button class="btn small" onclick="AC.resolveConflict(decodeURIComponent('${encodeURIComponent(c.path)}'),'remote')">保留远端</button></div></div>
          <div class="split" style="margin-top:10px"><div><div class="muted small">本机版本</div><pre>${esc(c.local)}</pre></div><div><div class="muted small">远端版本</div><pre>${esc(c.remote)}</pre></div></div>
        </div>`).join("") : `<div class="empty" style="padding:22px">拉取时如果有冲突，会在这里显示两边内容，由你选择。</div>`}
      </div>`;
  }

  async function saveDevicePath() {
    try {
      const data = await api(`/api/projects/${S.project.id}/path`, { method: "PUT", body: { path: $("#device-path").value } });
      S.project = data.project; toast("路径已保存"); await renderDevices();
    } catch (e) { toast(e.message, true); }
  }
  async function setRemote() {
    try { await api(`/api/projects/${S.project.id}/sync/remote`, { method: "POST", body: { url: $("#sync-remote").value } }); toast("remote 已保存"); await renderDevices(); }
    catch (e) { toast(e.message, true); }
  }
  async function syncAction(action) {
    try {
      const data = await api(`/api/projects/${S.project.id}/sync/${action}`, { method: "POST", body: {} });
      const result = data.result || data;
      if (result.ok === false || (result.conflicts && result.conflicts.length)) toast("拉取产生冲突，请在下方处理", true);
      else toast("同步完成");
      await renderDevices();
    } catch (e) { toast(e.message, true); }
  }
  async function resolveConflict(path, choice) {
    try { await api(`/api/projects/${S.project.id}/sync/conflicts/resolve`, { method: "POST", body: { path, choice } }); toast(`已保留${choice === "local" ? "本机" : "远端"}版本`); await renderDevices(); }
    catch (e) { toast(e.message, true); }
  }

  // --------------------------------------------------------------- settings

  async function renderSettings() {
    const setup = await api("/api/setup");
    S.setup = setup; S.settings = setup.settings;
    let backups = [];
    try { backups = (await api(`/api/projects/${S.project.id}/backups`)).backups; } catch {}
    const c = S.settings.claude || {};
    const x = S.settings.codex || {};
    $("#content").innerHTML = `${pageHead("设置", "检测运行时；配置真实 CLI 命令与 MagPie 连接。")}
      <div class="grid two">
        <div class="card"><h2>环境检查</h2>${setup.checks.map(ch => `<div class="check ${ch.ok ? "ok" : "bad"}"><span class="dot"></span><div><div>${esc(ch.label)}</div><div class="muted small">${esc(ch.detail)}</div>${!ch.ok && ch.fix ? `<div class="fix">${esc(ch.fix)}</div>` : ""}</div></div>`).join("")}</div>
        <div class="card"><h2>宿主</h2>${(setup.hosts || []).map(h => `<div class="check ${h.available ? "ok" : "bad"}"><span class="dot"></span><div><div>${esc(h.host)}</div><div class="muted small">${esc(h.version || "未找到")}</div></div></div>`).join("")}<div class="hr"></div><div class="muted small">数据目录：<code>${esc(setup.home || "")}</code></div></div>
      </div>
      <div class="grid two" style="margin-top:16px">
        <div class="card"><h2>Claude Code</h2>
          <div class="field"><span>命令</span><input id="set-claude-command" value="${esc(c.command || "claude")}"></div>
          <div class="field"><span>MagPie / API base URL</span><input id="set-claude-base" value="${esc(c.base_url || "")}" placeholder="http://127.0.0.1:3425"></div>
          <div class="field"><span>Token（显示为 ********）</span><input id="set-claude-token" type="password" value="${esc(c.token || "")}" placeholder="留空使用现有登录"></div>
          <div class="field"><span>模型</span><input id="set-claude-model" value="${esc(c.model || "")}" placeholder="group/claude-desktop-priority"></div>
          <button class="btn primary" onclick="AC.saveSettings()">保存 Claude 设置</button>
        </div>
        <div class="card"><h2>Codex</h2>
          <div class="field"><span>命令</span><input id="set-codex-command" value="${esc(x.command || "codex")}"></div>
          <div class="field"><span>CODEX_HOME（可选）</span><input id="set-codex-home" value="${esc(x.codex_home || "")}" placeholder="留空沿用环境变量"></div>
          <div class="field"><span>模型（可选）</span><input id="set-codex-model" value="${esc(x.model || "")}" placeholder="传 codex -m；留空沿用 Codex 自己的配置"></div>
          <div class="field"><span>Windows 沙箱（可选）</span><select id="set-codex-sandbox">
            <option value="" ${!x.windows_sandbox ? "selected" : ""}>沿用 Codex 配置</option>
            <option value="elevated" ${x.windows_sandbox === "elevated" ? "selected" : ""}>elevated（沙箱拒绝读项目时改用）</option>
            <option value="unelevated" ${x.windows_sandbox === "unelevated" ? "selected" : ""}>unelevated</option>
          </select></div>
          <div class="field"><span>接续任务超时（秒）</span><input id="set-hosts-timeout" type="number" min="60" step="60" value="${esc((S.settings.hosts && S.settings.hosts.timeout) || 900)}"></div>
          <div class="field"><span>接续工作区根目录（每个项目在其中拥有一个固定子目录）</span><input id="set-hosts-workspace" value="${esc((S.settings.hosts && S.settings.hosts.workspace) || (setup.workspace || ""))}" placeholder="${esc(setup.workspace || "")}"></div>
          <div class="row wrap gap"><button class="btn small" onclick="AC.openWorkspace()">打开当前项目工作区</button><button class="btn small" onclick="AC.syncWorkspace()">检查并回传</button></div>
          <p class="muted small" style="margin-top:6px">当前项目工作区：<code>${esc((setup.workspace || "") + (S.project ? " / " + S.project.id : ""))}</code>。工作区不能与项目目录相同或互相包含，否则启动前会报错。</p>
          <button class="btn primary" onclick="AC.saveSettings()">保存 Codex 设置</button>
          <div class="hr"></div>
          <h3>数据</h3>
          <div class="row wrap gap"><button class="btn" onclick="AC.exportData()">导出 JSON</button><button class="btn" onclick="AC.backupData()">备份 ZIP</button><button class="btn danger" onclick="AC.unintegrate()">移除宿主集成</button></div>
          <p class="muted small" style="margin-top:10px">卸载/移除只清理本产品写入的受管区段，保留项目与数据。</p>
          <div class="hr"></div>
          <h3>备份与恢复</h3>
          ${backups.length ? backups.slice(0, 5).map(b => `<div class="check"><span class="dot"></span><div style="flex:1"><div>${esc(b.name)}</div><div class="muted small">${Math.round(b.size / 1024)} KB · ${fmt(b.created_at)}</div></div><button class="btn small" onclick="AC.restoreBackup('${encodeURIComponent(b.name)}')">恢复</button></div>`).join("") : `<div class="muted small">还没有备份。点“备份 ZIP”生成一份。</div>`}
          <div class="hr"></div>
          <h3>项目登记</h3>
          <button class="btn danger small" onclick="AC.removeProject()">移除本项目的应用登记</button>
          <p class="muted small" style="margin-top:8px">只从本应用移除；源项目目录和数据仓库不会被删除。</p>
        </div>
      </div>`;
  }

  async function saveSettings() {
    try {
      const payload = { claude: {}, codex: {} };
      payload.claude.command = $("#set-claude-command").value;
      payload.claude.base_url = $("#set-claude-base").value;
      payload.claude.token = $("#set-claude-token").value;
      payload.claude.model = $("#set-claude-model").value;
      payload.codex.command = $("#set-codex-command").value;
      payload.codex.codex_home = $("#set-codex-home").value;
      payload.codex.model = $("#set-codex-model").value.trim();
      payload.codex.windows_sandbox = $("#set-codex-sandbox").value;
      payload.hosts = { timeout: parseInt($("#set-hosts-timeout").value, 10) || 900,
                        workspace: $("#set-hosts-workspace").value.trim() };
      const data = await api("/api/settings", { method: "PUT", body: payload });
      S.settings = data.settings; toast("设置已保存"); await renderSettings();
    } catch (e) { toast(e.message, true); }
  }
  async function exportData() {
    try {
      const data = await api(`/api/projects/${S.project.id}/export`);
      const blob = new Blob([JSON.stringify(data.data, null, 2)], { type: "application/json" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${S.project.id}-export.json`; a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { toast(e.message, true); }
  }
  async function backupData() {
    try { const data = await api(`/api/projects/${S.project.id}/backup`, { method: "POST" }); toast(`已备份到 ${data.path}`); }
    catch (e) { toast(e.message, true); }
  }
  async function restoreBackup(name) {
    const decoded = decodeURIComponent(name);
    if (!confirm(`从 ${decoded} 恢复数据？当前数据仓库会保留 Git 历史，但当前文件会被覆盖。`)) return;
    try {
      await api(`/api/projects/${S.project.id}/restore`, { method: "POST", body: { name: decoded } });
      toast("已从备份恢复"); await renderSettings();
    } catch (e) { toast(e.message, true); }
  }

  async function unintegrate() {
    if (!confirm("移除本产品写入 CLAUDE.md / AGENTS.md 的受管区段？项目文件的其他内容保留。")) return;
    try { await api(`/api/projects/${S.project.id}/unintegrate`, { method: "POST" }); toast("已移除宿主集成"); await renderSettings(); }
    catch (e) { toast(e.message, true); }
  }

  // ---------------------------------------------------------------- projects

  function openNewProject() {
    openModal(`<h2>新建项目</h2><p class="muted">登记一个本机目录；数据会写入独立的数据仓库。</p>
      <div class="field"><span>项目名称</span><input id="np-name" placeholder="例如：paper-pipeline"></div>
      <div class="field"><span>本机项目目录</span><input id="np-path" placeholder="E:\\work\\my-project"></div>
      <div class="modal-actions"><button class="btn ghost" onclick="AC.closeModal()">取消</button><button class="btn primary" onclick="AC.createProject()">创建</button></div>`);
  }
  function openJoinProject() {
    openModal(`<h2>加入已有项目</h2><p class="muted">克隆另一台设备的数据仓库，并映射到本机项目目录。</p>
      <div class="field"><span>数据仓库 Git 地址</span><input id="jp-remote" placeholder="git@github.com:you/ac-data.git"></div>
      <div class="field"><span>本机项目目录</span><input id="jp-path" placeholder="E:\\work\\my-project"></div>
      <div class="modal-actions"><button class="btn ghost" onclick="AC.closeModal()">取消</button><button class="btn primary" onclick="AC.joinProject()">加入</button></div>`);
  }
  async function removeProject() {
    if (!confirm("只移除本应用的项目登记？源项目和数据仓库不会被删除。")) return;
    try {
      await api(`/api/projects/${S.project.id}`, { method: "DELETE" });
      S.projects = S.projects.filter(p => p.id !== S.project.id);
      S.project = S.projects[0] || null;
      if (S.project) localStorage.setItem("ac-project", S.project.id); else localStorage.removeItem("ac-project");
      toast("已移除项目登记"); await render();
    } catch (e) { toast(e.message, true); }
  }

  async function createProject() {
    try {
      const data = await api("/api/projects", { method: "POST", body: { name: $("#np-name").value, path: $("#np-path").value } });
      closeModal(); S.projects.push(data.project); S.project = data.project; localStorage.setItem("ac-project", S.project.id); toast("项目已创建"); await render();
    } catch (e) { toast(e.message, true); }
  }
  async function joinProject() {
    try {
      const data = await api("/api/projects/join", { method: "POST", body: { remote: $("#jp-remote").value, path: $("#jp-path").value } });
      closeModal(); S.projects.push(data.project); S.project = data.project; localStorage.setItem("ac-project", S.project.id); toast("已加入项目"); await render();
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------------- wiring

  $("#project-select").addEventListener("change", (e) => selectProject(e.target.value));
  document.addEventListener("click", (e) => {
    const tab = e.target && e.target.closest ? e.target.closest(".tab") : null;
    if (tab && tab.dataset.tab) { e.preventDefault(); setTab(tab.dataset.tab); }
  });
  $("#btn-refresh").addEventListener("click", refreshAll);
  $("#btn-new-project").addEventListener("click", () => {
    openModal(`<h2>新建或加入项目</h2><p class="muted">第一次使用：新建一个项目；已有另一台设备的数据：加入它。</p>
      <div class="row gap"><button class="btn primary" onclick="AC.openNewProject()">新建项目</button><button class="btn" onclick="AC.openJoinProject()">加入已有项目</button></div>
      <div class="modal-actions"><button class="btn ghost" onclick="AC.closeModal()">取消</button></div>`);
  });

  async function refreshAll() {
    try {
      const [setup, projects] = await Promise.all([api("/api/setup"), api("/api/projects")]);
      S.setup = setup; S.projects = projects.projects;
      if (!S.project || !S.projects.find(p => p.id === S.project.id)) S.project = S.projects[0] || null;
      if (S.project) await refreshProject();
      renderChrome(); await render(); toast("已刷新");
    } catch (e) { toast(e.message, true); }
  }

  window.AC = {
    render, setTab, refreshAll, openNewProject, openJoinProject, createProject, joinProject,
    setDefAgent: (a) => { S.defAgent = a; renderDefinitions(); },
    showDefinition, saveDefinition, importDefinitionFile, activateDefinition, applyDefinition, restoreDefinition,
    addMemory, searchMemories, editMemory, saveMemory, setMemoryStatus, memoryHistory,
    createHandoff, continueHandoff, handoffDetail, pollJob,
    saveDevicePath, setRemote, syncAction, resolveConflict,
    saveSettings, exportData, backupData, restoreBackup, unintegrate, removeProject,
    openWorkspace, syncWorkspace, closeModal,
  };

  boot();
})();
