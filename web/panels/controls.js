// title: Controls
// Every setting in her config files: switches for on/off, boxes for numbers and text. A change is saved at once and she uses it on her next message.

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

export function mount(el, api) {
  el.innerHTML = `
    <style>
      #ctl { padding: 16px; display: grid; gap: 10px; align-content: start; max-width: 980px; }
      #bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
      #bar input { flex: 1; min-width: 180px; background: var(--bg); color: var(--text); border: 1px solid var(--line); padding: 6px 8px; font: inherit; }
      #bar button { background: none; border: 1px solid var(--accent); color: var(--accent); font: inherit; padding: 5px 12px; cursor: pointer; }
      #note { color: var(--dim); font-size: 12px; }
      details.file { background: var(--panel); border: 1px solid var(--line); padding: 4px 14px; }
      details.file > summary { cursor: pointer; padding: 8px 0; color: var(--accent); }
      .sec { margin: 8px 0 2px; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--dim); border-top: 1px solid var(--line); padding-top: 8px; }
      .hint { color: var(--dim); font-size: 11px; margin: 2px 0 6px; }
      .row { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(140px, 1.2fr) 70px; gap: 10px; align-items: center; padding: 4px 0; }
      .row .k { word-break: break-word; }
      .row input[type=text], .row input[type=number] { width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--line); padding: 4px 6px; font: inherit; }
      .row .st { font-size: 11px; color: var(--ok); }
      .row .st.bad { color: var(--warn); }
      .row.hide { display: none; }
      .sw { position: relative; width: 38px; height: 20px; display: inline-block; }
      .sw input { opacity: 0; width: 0; height: 0; }
      .sw span { position: absolute; inset: 0; background: var(--line); border-radius: 20px; cursor: pointer; transition: .15s; }
      .sw span::before { content: ""; position: absolute; width: 14px; height: 14px; left: 3px; top: 3px; background: var(--text); border-radius: 50%; transition: .15s; }
      .sw input:checked + span { background: var(--ok); }
      .sw input:checked + span::before { transform: translateX(18px); background: var(--bg); }
      .sw input:focus-visible + span { outline: 2px solid var(--accent); }
      @media (max-width: 640px) { .row { grid-template-columns: 1fr 1fr; } .row .st { grid-column: 1 / -1; } }
    </style>
    <div id="ctl">
      <div id="bar"><input id="find" placeholder="find a setting…"><button id="reload">reload</button></div>
      <div id="note">Saved the moment you change it, to the config files. Most settings apply on her next message. The under-18 picture rule is built in and has no switch.</div>
      <div id="out">loading…</div>
    </div>`;
  const out = el.querySelector("#out"), find = el.querySelector("#find");

  const save = async (row, body, parse) => {
    const st = row.querySelector(".st");
    try {
      const r = await api.call({ action: "set", ...body });
      st.textContent = "saved"; st.className = "st";
      setTimeout(() => (st.textContent = ""), 1500);
      return r;
    } catch (e) { st.textContent = e.message; st.className = "st bad"; }
  };

  const control = (file, path, v) => {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.key = (file + " " + path.join(" ")).toLowerCase();
    const label = `<div class="k">${esc(path[path.length - 1])}</div>`;
    if (typeof v === "boolean") {
      row.innerHTML = `${label}<label class="sw"><input type="checkbox" ${v ? "checked" : ""}><span></span></label><div class="st"></div>`;
      const box = row.querySelector("input");
      box.onchange = async () => { if (!(await save(row, { file, path, value: box.checked }))) box.checked = !box.checked; };
    } else if (typeof v === "number") {
      row.innerHTML = `${label}<input type="number" step="any" value="${esc(v)}"><div class="st"></div>`;
      const inp = row.querySelector("input");
      inp.onchange = () => save(row, { file, path, value: Number(inp.value) });
    } else if (Array.isArray(v)) {
      row.innerHTML = `${label}<input type="text" value="${esc(JSON.stringify(v))}"><div class="st"></div>`;
      const inp = row.querySelector("input");
      inp.onchange = () => { try { save(row, { file, path, value: JSON.parse(inp.value) }); } catch { const st = row.querySelector(".st"); st.textContent = "not a valid list"; st.className = "st bad"; } };
    } else {
      row.innerHTML = `${label}<input type="text" value="${esc(v)}"><div class="st"></div>`;
      const inp = row.querySelector("input");
      inp.onchange = () => save(row, { file, path, value: inp.value });
    }
    return row;
  };

  const section = (parent, file, tree, path, hints) => {
    for (const [k, v] of Object.entries(tree)) {
      if (v && typeof v === "object" && !Array.isArray(v)) {
        const title = document.createElement("div");
        title.className = "sec"; title.textContent = [...path, k].join(" › ");
        parent.append(title);
        const h = hints[[...path, k].join("/")];
        if (h) { const d = document.createElement("div"); d.className = "hint"; d.textContent = h; parent.append(d); }
        section(parent, file, v, [...path, k], hints);
      } else parent.append(control(file, [...path, k], v));
    }
  };

  const filter = () => {
    const q = find.value.trim().toLowerCase();
    out.querySelectorAll(".row").forEach((r) => r.classList.toggle("hide", !!q && !r.dataset.key.includes(q)));
    if (q) out.querySelectorAll("details.file").forEach((d) => (d.open = [...d.querySelectorAll(".row")].some((r) => !r.classList.contains("hide"))));
  };

  const load = async () => {
    try {
      const r = await api.call({ action: "list" });
      out.innerHTML = "";
      if (r.runtime.length) {
        const d = document.createElement("details");
        d.className = "file"; d.open = true;
        d.innerHTML = `<summary>runtime (memory only)</summary>`;
        for (const it of r.runtime) {
          const row = document.createElement("div");
          row.className = "row"; row.dataset.key = it.label.toLowerCase();
          row.innerHTML = `<div class="k">${esc(it.label)}</div><label class="sw"><input type="checkbox" ${it.value ? "checked" : ""}><span></span></label><div class="st"></div>`;
          const box = row.querySelector("input");
          box.onchange = async () => { if (!(await save(row, { runtime: it.id, value: box.checked }))) box.checked = !box.checked; };
          d.append(row);
        }
        out.append(d);
      }
      for (const f of r.files) {
        const d = document.createElement("details");
        d.className = "file";
        d.innerHTML = `<summary>${esc(f.file)}</summary>`;
        const top = f.hints[""];
        if (top) { const h = document.createElement("div"); h.className = "hint"; h.textContent = top; d.append(h); }
        section(d, f.file, f.tree, [], f.hints);
        out.append(d);
      }
      filter();
    } catch (e) { out.textContent = `couldn't read the settings: ${e.message}`; }
  };

  find.oninput = filter;
  el.querySelector("#reload").onclick = load;
  window.addEventListener("panel-shown", (e) => e.detail === "controls" && !out.querySelector(":focus") && load());
  load();
}
