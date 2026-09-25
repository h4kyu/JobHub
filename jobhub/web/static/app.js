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
    const src = e.target.closest("button.src");
    if (src) { post(`/api/sources/${src.dataset.repo}/${src.dataset.action}`).then(() => location.reload()); return; }
    const co = e.target.closest("button.co");
    if (co) { post(`/api/companies/${co.dataset.slug}/${co.dataset.action}`).then(() => location.reload()); return; }
    const task = e.target.closest("button.task");
    if (task) { startTask(task.dataset.kind, JSON.parse(task.dataset.args || "{}")); return; }
    const show = e.target.closest("a.showlog");
    if (show) { e.preventDefault(); followLog(show.dataset.id); }
  });

  const addco = document.getElementById("addco");
  if (addco) addco.addEventListener("submit", e => {
    e.preventDefault();
    const f = new FormData(addco);
    post("/api/companies", Object.fromEntries(f.entries())).then(r => r.error ? alert(r.error) : location.reload());
  });
  const disc = document.getElementById("discover");
  if (disc) disc.addEventListener("submit", e => {
    e.preventDefault();
    const f = new FormData(disc);
    startTask("discover", {angles: [f.get("angle")], like: f.get("like"), max_angles: f.get("max_angles"),
                           model_resolve: disc.elements.model_resolve.checked}).then(id => { if (id) location.href = "/runs"; });
  });

  // Task forms on the Runs page: every field is an option of the same-named CLI flag.
  document.addEventListener("submit", e => {
    const f = e.target.closest("form.taskform");
    if (!f) return;
    e.preventDefault();
    const args = {};
    new FormData(f).forEach((v, k) => { if (v !== "") args[k] = v; });
    f.querySelectorAll("input[type=checkbox]").forEach(c => { args[c.name] = c.checked; });
    if (args.angle_text) { args.angles = [args.angle_text]; delete args.angle_text; }
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
      if (location.pathname === "/runs") followLog(r.id); else location.href = "/runs";
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
