const JobHub = (() => {
  const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})}).then(r => r.json());

  function setStatus(id, status, notes, el) {
    return post("/api/status", {job_id: id, status, notes}).then(res => {
      if (res.error) { alert(res.error); return; }
      if (el) { el.classList.add("changed"); const sel = el.querySelector("select.status"); if (sel) sel.value = status; }
    });
  }

  document.addEventListener("change", e => {
    if (e.target.matches("select.status")) setStatus(e.target.dataset.id, e.target.value, null, e.target.closest(".jobcard"));
  });
  document.addEventListener("click", e => {
    const b = e.target.closest(".quick button");
    if (b) { const card = b.closest(".jobcard"); setStatus(card.dataset.id, b.dataset.status, null, card); return; }
    const save = e.target.closest("#savestatus");
    if (save) {
      setStatus(save.dataset.id, document.getElementById("jobstatus").value, document.getElementById("jobnotes").value)
        .then(() => { document.getElementById("saved").textContent = "saved"; });
      return;
    }
    const task = e.target.closest("button.task");
    if (task) { startTask(task.dataset.kind, JSON.parse(task.dataset.args || "{}")); return; }
  });

  // Watchlist page: search the company directory, add a result, remove a followed company.
  const wlq = document.getElementById("wlq"), wlres = document.getElementById("wlresults");
  if (wlq) {
    let tag = "", seq = 0, timer0 = null;
    const note = text => { wlres.replaceChildren(Object.assign(document.createElement("p"), {className: "hint", textContent: text})); };
    const render = (d, q) => {
      if (!d.results.length) { note(q ? `We don't recognize “${q}”. Check the spelling.` : "Nothing in that field yet."); return; }
      const rows = d.results.map(r => {
        const row = document.createElement("div"); row.className = "wl-row";
        const name = document.createElement("span"); name.className = "wl-name"; name.textContent = r.name;
        const tags = document.createElement("span"); tags.className = "wl-tags";
        r.tags.forEach(t => { const b = document.createElement("span"); b.className = "badge"; b.textContent = t; tags.append(b); });
        if (!r.in_directory) { const b = document.createElement("span"); b.className = "badge"; b.textContent = "from job lists"; tags.append(b); }
        const act = document.createElement("button"); act.type = "button";
        act.textContent = r.on ? "✓ Added" : "Add"; act.disabled = r.on; act.className = r.on ? "ghost" : "primary";
        act.addEventListener("click", () => {
          act.disabled = true;
          post("/api/watchlist", {key: r.key, directory: r.in_directory}).then(res => res.error ? (alert(res.error), act.disabled = false) : location.reload());
        });
        row.append(name, tags, act); return row;
      });
      if (d.more) rows.push(Object.assign(document.createElement("p"), {className: "hint", textContent: `${d.more} more — type to narrow it down.`}));
      wlres.replaceChildren(...rows);
    };
    const search = () => {
      const q = wlq.value.trim(), mine = ++seq;
      if (!q && !tag) { wlres.replaceChildren(); return; }
      fetch(`/api/directory/search?q=${encodeURIComponent(q)}&tag=${encodeURIComponent(tag)}`).then(r => r.json())
        .then(d => { if (mine === seq) render(d, q); });
    };
    wlq.addEventListener("input", () => { clearTimeout(timer0); timer0 = setTimeout(search, 150); });
    document.querySelectorAll("#wltags .chip").forEach(c => c.addEventListener("click", () => {
      tag = tag === c.dataset.tag ? "" : c.dataset.tag;
      document.querySelectorAll("#wltags .chip").forEach(x => x.classList.toggle("on", x.dataset.tag === tag));
      search();
    }));
  }
  document.addEventListener("click", e => {
    const x = e.target.closest("button.wl-x, button.wl-remove");
    if (x) post(`/api/watchlist/${x.dataset.slug}/remove`).then(() => location.reload());
  });

  // Pipeline page, Sources stage: the count tiles open the lists/boards they count; lists can be switched or added.
  const openTile = t => { const d = document.getElementById(t.dataset.open); if (d) { d.open = !d.open; if (d.open) d.scrollIntoView({block: "nearest"}); } };
  document.querySelectorAll(".metrics .open").forEach(t => {
    t.addEventListener("click", () => openTile(t));
    t.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTile(t); } });
  });
  document.addEventListener("click", e => {
    const b = e.target.closest("button.src");
    if (b) post(`/api/sources/${b.dataset.repo}/${b.dataset.action}`).then(() => location.reload());
  });
  const addlist = document.getElementById("addlist");
  if (addlist) addlist.addEventListener("submit", e => {
    e.preventDefault();
    const msg = document.getElementById("addlistmsg"), btn = addlist.querySelector("button");
    btn.disabled = true; msg.textContent = "Checking the repo…";
    post("/api/sources", {repo: addlist.elements.repo.value}).then(r => {
      if (r.error) { msg.textContent = r.error; btn.disabled = false; } else location.reload();
    });
  });
  const boardfilter = document.getElementById("boardfilter");
  if (boardfilter) boardfilter.addEventListener("input", () => {
    const q = boardfilter.value.trim().toLowerCase();
    document.querySelectorAll("#boardrows tr").forEach(r => { r.hidden = q && !r.dataset.name.includes(q); });
  });
  const wlf = document.getElementById("wlfilter");
  if (wlf) wlf.addEventListener("input", () => {
    const q = wlf.value.trim().toLowerCase();
    let n = 0;
    document.querySelectorAll("#wllist .wl-item").forEach(i => { const show = !q || i.dataset.name.includes(q); i.hidden = !show; n += show; });
    document.getElementById("wlcount").textContent = n;
  });

  // Task forms on the Runs page: every field is an option of the same-named CLI flag.
  document.addEventListener("submit", e => {
    const f = e.target.closest("form.taskform");
    if (!f) return;
    e.preventDefault();
    const args = {};
    new FormData(f).forEach((v, k) => { if (v !== "") args[k] = v; });
    f.querySelectorAll("input[type=checkbox]").forEach(c => { args[c.name] = c.checked; });
    startTask(f.dataset.kind, args);
  });

  // Pipeline page: edit a whitelisted knob in profile.yaml (never a model-visible one, so nothing re-evaluates).
  const saveKnob = el => {
    const key = el.dataset.knob, value = el.type === "checkbox" ? el.checked : el.value;
    post("/api/settings", {key, value}).then(r => {
      if (r.error) { alert(r.error); return; }
      el.classList.add("saved"); setTimeout(() => location.reload(), 500);
    });
  };
  document.addEventListener("click", e => {
    const b = e.target.closest("button.saveknob");
    if (b) saveKnob(document.querySelector(`input.knobinput[data-knob="${b.dataset.knob}"]`));
  });
  document.addEventListener("change", e => { if (e.target.matches('input.knobinput[type=checkbox]')) saveKnob(e.target); });

  // Jobs page: tick jobs, then run the full rubric on just those.
  const picked = () => [...document.querySelectorAll("input.pick:checked")].map(c => Number(c.value));
  document.addEventListener("change", e => {
    if (!e.target.matches("input.pick")) return;
    const n = picked().length, btn = document.getElementById("deepsel");
    document.getElementById("npick").textContent = n;
    if (btn) btn.disabled = n === 0;
  });
  const deepsel = document.getElementById("deepsel");
  if (deepsel) deepsel.addEventListener("click", () => startTask("deep", {job_ids: picked()}));

  function startTask(kind, args) {
    return post(`/api/tasks/${kind}`, args).then(r => {
      if (r.error) { alert(r.error); return null; }
      if (location.pathname === "/pipeline") { followLog(r.id); document.getElementById("livelog")?.scrollIntoView({block: "nearest"}); } else location.href = "/pipeline";
      return r.id;
    });
  }

  let timer = null;
  function followLog(id) {
    const box = document.getElementById("livelog"), pre = document.getElementById("logtext"), st = document.getElementById("logstatus");
    if (!box) return;
    box.classList.add("show");
    clearInterval(timer);
    const tick = () => fetch(`/api/tasks/${id}/log`).then(r => r.json()).then(d => {
      pre.textContent = d.log || ""; pre.scrollTop = pre.scrollHeight;
      st.textContent = d.status === "running" ? "· running" : `· ${d.status}`;
      const bar = document.getElementById("taskbar");
      if (d.status !== "running") { clearInterval(timer); if (bar) bar.classList.remove("show"); }
    });
    tick(); timer = setInterval(tick, 2000);
  }

  return {followLog, startTask};
})();
