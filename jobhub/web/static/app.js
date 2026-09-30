const JobHub = (() => {
  const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})}).then(r => r.json());

  function setStatus(id, status, notes, el) {
    return post("/api/status", {job_id: id, status, notes}).then(res => {
      if (res.error) { alert(res.error); return; }
      if (el) { el.classList.add("changed"); const sel = el.querySelector("select.status"); if (sel) sel.value = status; }
      const detailSelect = document.querySelector(`#jobstatus[data-id="${id}"]`);
      if (detailSelect) detailSelect.value = status;
    });
  }

  document.addEventListener("change", e => {
    if (e.target.matches("select.status")) setStatus(e.target.dataset.id, e.target.value, null, e.target.closest(".jobcard"));
  });
  document.addEventListener("click", e => {
    const b = e.target.closest(".quick button");
    if (b) { const card = b.closest(".jobcard"), id = b.dataset.id || (card && card.dataset.id); setStatus(id, b.dataset.status, null, card); return; }
    const companyWarning = e.target.closest(".company-warning-toggle, .company-warning-skip");
    if (companyWarning) {
      const skip = companyWarning.classList.contains("company-warning-skip");
      post("/api/company-us-work-auth-warning", {
        job_id: Number(companyWarning.dataset.id),
        enabled: skip || companyWarning.dataset.enabled === "true",
        skip
      }).then(res => res.error ? alert(res.error) : location.reload());
      return;
    }
    const task = e.target.closest("button.task");
    if (task) { startTask(task.dataset.kind, JSON.parse(task.dataset.args || "{}")); return; }
  });

  // Job detail: status changes are immediate; notes save after typing pauses (and on blur).
  const jobNotes = document.getElementById("jobnotes"), jobStatus = document.getElementById("jobstatus"), saved = document.getElementById("saved");
  if (jobNotes && jobStatus) {
    let notesTimer = null;
    const saveNotes = () => {
      clearTimeout(notesTimer);
      if (jobNotes.value === jobNotes.defaultValue) return;
      if (saved) saved.textContent = "saving…";
      setStatus(jobStatus.dataset.id, jobStatus.value, jobNotes.value).then(() => {
        jobNotes.defaultValue = jobNotes.value;
        if (saved) saved.textContent = "saved";
      });
    };
    jobNotes.addEventListener("input", () => { if (saved) saved.textContent = ""; clearTimeout(notesTimer); notesTimer = setTimeout(saveNotes, 650); });
    jobNotes.addEventListener("blur", saveNotes);
  }

  // Listing cards navigate as one large target, except where an actual control/link was clicked.
  const openCard = (card) => { if (card && card.dataset.href) location.href = card.dataset.href; };
  document.addEventListener("click", e => {
    const card = e.target.closest(".jobcard[data-href]");
    if (!card || e.target.closest("a, button, input, select, textarea, label")) return;
    openCard(card);
  });
  document.addEventListener("keydown", e => {
    const card = e.target.closest && e.target.closest(".jobcard[data-href]");
    if (!card || e.target !== card || !["Enter", " "].includes(e.key)) return;
    e.preventDefault(); openCard(card);
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
    const c = document.getElementById("wlcount"); if (c) c.textContent = n;
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
  document.addEventListener("change", e => { if (e.target.matches("input.knobinput")) saveKnob(e.target); });

  // Jobs page: the filter drawer stays shut unless something is on, so the header is one row.
  const jfilt = document.querySelector("button.jfilt"), jdrawer = document.getElementById("jdrawer");
  if (jfilt && jdrawer) {
    if (jfilt.classList.contains("on")) { jdrawer.hidden = false; jfilt.setAttribute("aria-expanded", "true"); }
    jfilt.addEventListener("click", () => {
      jdrawer.hidden = !jdrawer.hidden;
      jfilt.setAttribute("aria-expanded", String(!jdrawer.hidden));
    });
  }

  // Profile: save a field when it loses focus, one at a time so a bad value can't sink the rest.
  const flash = (el, cls) => { el.classList.add(cls); setTimeout(() => el.classList.remove(cls), 1400); };
  const saveField = el => {
    const key = el.dataset.field;
    const value = el.dataset.kind === "bool" ? el.checked
                : el.dataset.kind === "list" ? el.value.split(",").map(s => s.trim()).filter(Boolean)
                : el.value;
    const note = document.getElementById("pfsave");
    return post("/api/profile", {key, value}).then(r => {
      if (r.error) { flash(el, "bad"); if (note) note.textContent = r.error; return; }
      flash(el, "saved");
      if (el.dataset.kind === "bool") { const s = el.parentElement.querySelector("span"); if (s) s.textContent = el.checked ? "on" : "off"; }
      if (note) note.textContent = r.rescore ? "Saved — re-score to apply it to existing postings." : "Saved.";
    });
  };
  document.addEventListener("change", e => {
    if (e.target.matches("[data-field]") && e.target.dataset.kind === "bool") saveField(e.target);
  });
  document.addEventListener("blur", e => {
    const el = e.target;
    if (!el.matches || !el.matches("[data-field]") || el.dataset.kind === "bool") return;
    if (el.value !== el.defaultValue) { el.defaultValue = el.value; saveField(el); }
  }, true);

  // Theme picker: swap the attribute first so the choice is instant, then persist it.
  document.addEventListener("click", e => {
    const t = e.target.closest("button.theme");
    if (!t) return;
    const key = t.dataset.themeKey, prev = document.documentElement.dataset.theme;
    document.documentElement.dataset.theme = key;
    document.querySelectorAll("button.theme").forEach(b => b.classList.toggle("on", b === t));
    post("/api/theme", {theme: key}).then(r => {
      if (r && r.error) { alert(r.error); document.documentElement.dataset.theme = prev; }
    });
  });

  // Profile: the role-type list. Picks, weights and exclusions are one setting, saved together — the scorer
  // reads them together, and a half-applied edit (weights saved, exclusions not) would score against a state
  // the user never chose. So everything here is local until Save.
  const rt = document.getElementById("rt");
  if (rt) (() => {
    const picks = document.getElementById("rtpicks"), pick = document.getElementById("rtpick");
    const apply = document.getElementById("rtapply"), note = document.getElementById("rtnote");
    const chips = document.getElementById("rtexcluded"), search = document.getElementById("rtsearch");
    const lo = Number(rt.dataset.lo), dflt = Number(rt.dataset.default);
    let mode = "pick";              // what the catalog list is currently adding to: a pick or the exclusions
    const dirty = () => { apply.disabled = false; note.textContent = "Not saved yet."; };

    const mark = () => {            // grey out catalog entries already used, either way
      const used = new Set([...picks.querySelectorAll(".rtrow")].map(r => r.dataset.key)
                    .concat([...chips.querySelectorAll(".rtchip")].map(c => c.dataset.key)));
      pick.querySelectorAll(".rtopt").forEach(o => { o.disabled = used.has(o.dataset.key); });
      picks.querySelector("p.hint")?.remove();
      picks.querySelectorAll(".rtrow").forEach(r => {
        r.classList.toggle("weak", Number(r.querySelector(".rtw").value) <= lo);
      });
    };

    const row = ({key, label, weight, keywords, blurb, custom}) => {
      const el = document.createElement("div");
      el.className = "rtrow"; el.dataset.key = key; el.dataset.custom = custom ? "1" : "0";
      el.innerHTML = `<div class="rtmain"><span class="rtname"></span>${blurb ? "<small></small>" : ""}</div>
        <input class="rtw" type="range" min="0" max="100" step="1"><output class="rtval"></output>
        <button class="rtkw" type="button">words</button><button class="rtdel" type="button">&times;</button>
        <div class="rtwords" hidden><input type="text"${custom ? "" : " readonly"}><small></small></div>`;
      el.querySelector(".rtname").textContent = label;
      if (custom) el.querySelector(".rtname").insertAdjacentHTML("beforeend", '<span class="rtown">yours</span>');
      if (blurb) el.querySelector(".rtmain small").textContent = blurb;
      el.querySelector(".rtw").value = weight; el.querySelector(".rtval").textContent = weight;
      el.querySelector(".rtwords input").value = (keywords || []).join(", ");
      el.querySelector(".rtwords small").textContent = custom
        ? "Comma separated. A word must start and end with a letter or digit."
        : "From the shipped catalog — add your own type if you need different words.";
      return el;
    };

    rt.addEventListener("input", e => {
      if (!e.target.matches(".rtw, .rtwords input, #rtsearch")) return;
      if (e.target.matches("#rtsearch")) {
        const q = e.target.value.trim().toLowerCase();
        pick.querySelectorAll(".rtopt").forEach(o => {
          o.hidden = q && !(o.dataset.label + " " + o.dataset.blurb + " " + o.dataset.keywords).toLowerCase().includes(q);
        });
        pick.querySelectorAll(".rtgroup").forEach(g => {
          g.hidden = ![...g.querySelectorAll(".rtopt")].some(o => !o.hidden);
        });
        return;
      }
      if (e.target.matches(".rtw")) {
        const r = e.target.closest(".rtrow");
        r.querySelector(".rtval").textContent = e.target.value;
        r.classList.toggle("weak", Number(e.target.value) <= lo);
      }
      dirty();
    });

    rt.addEventListener("click", e => {
      const t = e.target;
      if (t.closest("#rtopen") || t.closest("#rtexadd")) {
        mode = t.closest("#rtexadd") ? "exclude" : "pick";
        pick.hidden = false; search.value = ""; search.dispatchEvent(new Event("input", {bubbles: true}));
        search.focus();
        return;
      }
      if (t.closest("#rtcustom")) {
        const label = prompt("What do you want to call it?");
        if (!label || !label.trim()) return;
        const key = "my-" + label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
        if (!key.replace(/^my-$/, "") || picks.querySelector(`[data-key="${key}"]`)) return;
        const el = row({key, label: label.trim(), weight: dflt, keywords: [], blurb: "", custom: true});
        picks.append(el); el.querySelector(".rtwords").hidden = false;
        el.querySelector(".rtwords input").focus();
        mark(); dirty();
        return;
      }
      const opt = t.closest(".rtopt");
      if (opt) {
        if (mode === "exclude") {
          const c = document.createElement("span");
          c.className = "rtchip"; c.dataset.key = opt.dataset.key;
          c.textContent = opt.dataset.label;
          c.insertAdjacentHTML("beforeend", '<button type="button" title="Remove">&times;</button>');
          chips.append(c);
        } else {
          picks.append(row({key: opt.dataset.key, label: opt.dataset.label, weight: dflt, blurb: opt.dataset.blurb,
                            keywords: opt.dataset.keywords ? opt.dataset.keywords.split(", ") : [], custom: false}));
        }
        pick.hidden = true; mark(); dirty();
        return;
      }
      if (t.closest(".rtkw")) { const w = t.closest(".rtrow").querySelector(".rtwords"); w.hidden = !w.hidden; return; }
      if (t.closest(".rtdel")) { t.closest(".rtrow").remove(); mark(); dirty(); return; }
      if (t.closest(".rtchip button")) { t.closest(".rtchip").remove(); mark(); dirty(); return; }
      if (t.closest("#rtapply")) {
        const body = {
          picks: [...picks.querySelectorAll(".rtrow")].map(r => {
            const p = {key: r.dataset.key, weight: Number(r.querySelector(".rtw").value)};
            if (r.dataset.custom === "1") {
              p.label = r.querySelector(".rtname").childNodes[0].textContent.trim();
              p.keywords = r.querySelector(".rtwords input").value.split(",").map(s => s.trim()).filter(Boolean);
            }
            return p;
          }),
          excluded: [...chips.querySelectorAll(".rtchip")].map(c => c.dataset.key),
        };
        apply.disabled = true; note.textContent = "Saving…";
        post("/api/roletypes", body).then(r => {
          if (r.error) { apply.disabled = false; note.textContent = r.error; return; }
          note.textContent = "Saved — re-score to apply it to existing postings.";
        });
      }
    });
    mark();
  })();

  // Profile: the prose the model reads. Saved explicitly, not on blur — it is a document being written, and
  // an autosave mid-sentence would re-score the board on a half-finished thought.
  const pmd = document.getElementById("pmd"), pmdsave = document.getElementById("pmdsave");
  if (pmd && pmdsave) {
    const pmdnote = document.getElementById("pmdnote");
    pmd.addEventListener("input", () => { pmdsave.disabled = pmd.value === pmd.defaultValue; });
    pmdsave.addEventListener("click", () => {
      pmdsave.disabled = true; pmdnote.textContent = "Saving…";
      post("/api/profile_md", {text: pmd.value}).then(r => {
        if (r.error) { pmdsave.disabled = false; pmdnote.textContent = r.error; return; }
        pmd.defaultValue = pmd.value;
        pmdnote.textContent = "Saved — re-score to apply it to existing postings.";
      });
    });
  }

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
