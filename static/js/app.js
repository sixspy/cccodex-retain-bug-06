(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行" || state === "执行中" || state === "裁撤中"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁"
          ? "ok"
          : state === "已失败"
            ? "bad"
            : state === "已作废"
              ? "off"
              : "";
    return `<span class="tag ${cls}">${state}</span>`;
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${p.domain_name}</td>
              <td class="mono">${p.domain_code}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。数字与后续对账同源。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${p.domain_code}</td>
              <td class="mono">${p.biz_date}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">${p.archive_uri || "—"}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.as_of_date}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.message}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」或「作废」。作废原子释放分区占用并写执行流水，作废单不可再执行。</p>
        <p class="form-msg" id="judge-msg"></p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => {
                        const canAct = j.status === "待执行" || j.status === "已失败";
                        const reason =
                          j.status === "已失败" && j.fail_reason
                            ? `<div class="sub">失败原因：${j.fail_reason}</div>`
                            : j.status === "已作废"
                              ? `<div class="sub">已作废，执行被拒绝</div>`
                              : "";
                        return `<tr class="${j.status === "已作废" ? "row-off" : ""}">
              <td>${j.id}</td>
              <td class="mono">${j.domain_code}</td>
              <td class="mono">${j.biz_date}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${j.decided_action}</td>
              <td>${tag(j.status)}${reason}</td>
              <td>${
                canAct
                  ? `<button type="button" class="mini" data-exec="${j.id}">执行</button>` +
                    (j.status === "待执行"
                      ? `<button type="button" class="mini ghost" data-cancel="${j.id}">作废</button>`
                      : "")
                  : "—"
              }</td>
            </tr>`;
                      }
                    )
                    .join("")
                : `<tr><td colspan="7" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    const msg = document.getElementById("judge-msg");
    const flash = (text, ok) => {
      msg.textContent = text;
      msg.className = `form-msg ${ok ? "ok" : "bad"}`;
    };
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
          await renderJudgments();
        } catch (err) {
          flash(err.message, false);
          btn.disabled = false;
        }
      };
    });
    main.querySelectorAll("[data-cancel]").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api(`/api/judgments/${btn.dataset.cancel}/cancel`, { method: "POST" });
          await renderJudgments();
        } catch (err) {
          flash(err.message, false);
          btn.disabled = false;
        }
      };
    });
  }

  async function renderRuns() {
    const data = await api("/api/runs");
    main.innerHTML = `
      <section class="panel">
        <h2>执行流水</h2>
        <p class="sub">执行足迹与裁决状态同源落库：归档/销毁成功、失败、作废各有一条可核记录，作废单结果态为「已作废」。</p>
        <table>
          <thead><tr><th>流水号</th><th>裁决编号</th><th>动作</th><th>结果态</th><th>是否成功</th><th>归档地址（放行详情）</th><th>说明</th><th>时间</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (r) => `<tr class="${r.result_state === "已作废" ? "row-off" : ""}">
              <td>${r.id}</td>
              <td class="mono">${r.judgment_id}</td>
              <td>${r.action}</td>
              <td>${tag(r.result_state)}</td>
              <td>${r.ok === 1 ? "成功" : r.result_state === "已作废" ? "未执行" : "失败"}</td>
              <td class="mono">${r.archive_uri || "—"}</td>
              <td>${r.detail || "—"}</td>
              <td class="mono">${r.created_at}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无执行流水</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${r.as_of_date}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td>${r.summary}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    runs: renderRuns,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
