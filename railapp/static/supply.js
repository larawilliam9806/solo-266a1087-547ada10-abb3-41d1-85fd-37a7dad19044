(() => {
  "use strict";

  const enableForm = document.getElementById("enable-form");
  const form = document.getElementById("supply-form");
  if (!form) return;

  const statusEl = document.getElementById("supply-status");
  const planEl = document.getElementById("supply-plan");
  const filesBody = document.getElementById("plan-files");
  const sourcesBody = document.getElementById("plan-sources");
  const exclusionsBody = document.getElementById("plan-exclusions");
  const snapshotAt = document.getElementById("snapshot-at");
  const downloadButton = document.getElementById("download-button");
  const startInput = document.getElementById("start");
  const endInput = document.getElementById("end");

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  const setStatus = (text, kind) => {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " " + kind : "");
  };

  if (enableForm) {
    enableForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const note = document.getElementById("enable-status");
      note.textContent = "启用中…";
      note.className = "status busy";
      try {
        const response = await fetch("/api/ridership-supply/enable", {
          method: "POST",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          note.textContent = "启用失败，请重试";
          note.className = "status bad";
          return;
        }
        window.location.reload();
      } catch (err) {
        note.textContent = "网络错误：" + err.message;
        note.className = "status bad";
      }
    });
  }

  let currentRange = null;

  function renderPlan(plan) {
    currentRange = { start: plan.travel_days[0], end: plan.travel_days[plan.travel_days.length - 1] };
    snapshotAt.textContent = plan.generated_at;

    filesBody.replaceChildren();
    plan.files.forEach((item) => {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", item.file));
      tr.appendChild(el("td", "", item.travel_date));
      tr.appendChild(el("td", "", item.row_count));
      filesBody.appendChild(tr);
    });

    sourcesBody.replaceChildren();
    if (!plan.manifest_sources.length) {
      const tr = el("tr");
      const td = el("td", "empty", "该区间内没有已登记的供数明细，各 CSV 将只含表头。");
      td.colSpan = 6;
      tr.appendChild(td);
      sourcesBody.appendChild(tr);
    }
    plan.manifest_sources.forEach((row) => {
      const tr = el("tr");
      row.forEach((value) => tr.appendChild(el("td", "mono", value)));
      sourcesBody.appendChild(tr);
    });

    exclusionsBody.replaceChildren();
    if (!plan.manifest_exclusions.length) {
      const tr = el("tr");
      const td = el("td", "empty", "所有当前正式批次均已纳入（或不存在其他批次）。");
      td.colSpan = 5;
      tr.appendChild(td);
      exclusionsBody.appendChild(tr);
    }
    plan.manifest_exclusions.forEach((row) => {
      const tr = el("tr");
      row.forEach((value, index) => tr.appendChild(el("td", index >= 3 ? "" : "mono", value)));
      exclusionsBody.appendChild(tr);
    });

    planEl.hidden = false;
    downloadButton.disabled = false;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const start = startInput.value;
    const end = endInput.value;
    if (!start || !end) {
      setStatus("请选择开始和结束乘车日。", "bad");
      return;
    }
    setStatus("读取已登记供数并生成清单…", "busy");
    planEl.hidden = true;
    downloadButton.disabled = true;
    try {
      const response = await fetch(
        `/api/ridership-supply/plan?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
        { headers: { Accept: "application/json" } }
      );
      if (!response.ok) {
        let message = `请求失败（HTTP ${response.status}）`;
        try {
          const payload = await response.json();
          if (payload.message) message = payload.message;
        } catch (_) { /* ignore */ }
        setStatus(message, "bad");
        return;
      }
      const plan = await response.json();
      setStatus("清单生成成功，可下载 ZIP。", "ok");
      renderPlan(plan);
    } catch (err) {
      setStatus("网络错误：" + err.message, "bad");
    }
  });

  downloadButton.addEventListener("click", () => {
    if (!currentRange) return;
    const url = `/ridership-supply.zip?start=${encodeURIComponent(currentRange.start)}`
      + `&end=${encodeURIComponent(currentRange.end)}`;
    window.location.href = url;
  });
})();
