// title: Images
// Make a picture with the Chroma model. Her language model steps out of the GPU while it draws and comes back afterwards.

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

export function mount(el, api) {
  el.innerHTML = `
    <style>
      #imgs { display: grid; grid-template-columns: minmax(280px, 380px) 1fr; gap: 16px; padding: 16px; align-items: start; }
      @media (max-width: 800px) { #imgs { grid-template-columns: 1fr; } }
      .form { background: var(--panel); border: 1px solid var(--line); padding: 14px; display: grid; gap: 10px; }
      .form label { display: grid; gap: 4px; color: var(--dim); font-size: 12px; }
      .form textarea, .form input, .form select { background: var(--bg); color: var(--text); border: 1px solid var(--line); padding: 6px 8px; font: inherit; width: 100%; }
      .form textarea:focus, .form input:focus, .form select:focus { outline: 1px solid var(--accent); }
      .two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
      #make { background: var(--accent); color: var(--bg); border: 0; padding: 9px; font: inherit; font-weight: bold; cursor: pointer; }
      #make:disabled { opacity: .5; cursor: wait; }
      #state { color: var(--dim); font-size: 12px; min-height: 18px; }
      #state.bad { color: var(--warn); }
      #grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
      .pic { background: var(--panel); border: 1px solid var(--line); }
      .pic img { display: block; width: 100%; height: auto; cursor: zoom-in; }
      .pic .meta { padding: 6px 8px; font-size: 11px; color: var(--dim); }
      .pic .meta button { background: none; border: 0; color: var(--accent); font: inherit; cursor: pointer; padding: 0; margin-right: 8px; }
      #empty { color: var(--dim); }
      details.ex { grid-column: 1 / -1; background: var(--panel); border: 1px solid var(--line); padding: 10px 14px; }
      details.ex summary { cursor: pointer; color: var(--accent); }
      .exrow { display: grid; grid-template-columns: minmax(120px, 360px) 1fr; gap: 12px; padding: 12px 0; border-top: 1px solid var(--line); margin-top: 10px; }
      @media (max-width: 800px) { .exrow { grid-template-columns: 1fr; } }
      .exrow .thumbs { display: flex; gap: 6px; align-items: flex-start; }
      .exrow .thumbs img { width: 100%; min-width: 0; display: block; }
      .exrow h4 { margin: 0 0 4px; }
      .exrow pre { white-space: pre-wrap; word-break: break-word; background: var(--bg); border: 1px solid var(--line); padding: 8px; margin: 6px 0; font: inherit; }
      .exrow .opts, .exrow .note { color: var(--dim); font-size: 12px; margin: 4px 0; }
      .exrow button { background: none; border: 1px solid var(--accent); color: var(--accent); font: inherit; padding: 3px 10px; cursor: pointer; }
    </style>
    <div id="imgs">
      <div class="form">
        <label>Prompt (plain sentences work best: subject, setting, light, style)<textarea id="prompt" rows="6"></textarea></label>
        <label>Avoid<textarea id="neg" rows="2" placeholder="blank = the model's usual list"></textarea></label>
        <div class="two">
          <label>Shape<select id="shape">
            <option value="1024x1024">square 1024×1024</option><option value="832x1216">portrait 832×1216</option>
            <option value="1216x832">landscape 1216×832</option><option value="768x1344">tall 768×1344</option><option value="1344x768">wide 1344×768</option>
          </select></label>
          <label>Steps<input id="steps" type="number" min="4" max="60" value="26"></label>
        </div>
        <div class="two">
          <label>Guidance<input id="cfg" type="number" min="1" max="12" step="0.5" value="3.8"></label>
          <label>Seed (blank = random)<input id="seed" type="number"></label>
        </div>
        <button id="make">Generate</button>
        <div id="state"></div>
      </div>
      <div><div id="grid"></div><div id="empty"></div></div>
      <details class="ex"><summary>Examples: pictures with the prompts that made them (click to open)</summary><div id="exlist">loading…</div></details>
    </div>`;
  const $ = (id) => el.querySelector(`#${id}`);
  const grid = $("grid"), state = $("state"), make = $("make");

  const card = (im) => {
    const d = document.createElement("div");
    d.className = "pic";
    d.innerHTML = `<a href="/images/${encodeURIComponent(im.name)}" target="_blank"><img loading="lazy" src="/images/${encodeURIComponent(im.name)}" alt="${esc(im.prompt)}"></a>
      <div class="meta"><button data-act="reuse">reuse</button>seed ${esc(im.seed)} · ${esc(im.width)}×${esc(im.height)}<br>${esc((im.prompt || "").slice(0, 120))}</div>`;
    d.querySelector('[data-act="reuse"]').onclick = () => {
      $("prompt").value = im.prompt || ""; $("neg").value = im.negative || ""; $("seed").value = im.seed ?? "";
      if (im.width && im.height) $("shape").value = `${im.width}x${im.height}`;
      if (im.steps) $("steps").value = im.steps; if (im.guidance) $("cfg").value = im.guidance;
    };
    return d;
  };
  const load = async () => {
    try {
      const r = await api.call({ action: "list" });
      grid.innerHTML = ""; r.images.forEach((im) => grid.append(card(im)));
      $("empty").textContent = r.images.length ? "" : "No pictures yet.";
      if (!r.ready) { state.textContent = "The picture model isn't installed."; state.className = "bad"; make.disabled = true; }
    } catch (e) { state.textContent = e.message; state.className = "bad"; }
  };

  const loadExamples = async () => {
    try {
      const r = await api.call({ action: "examples" });
      $("exlist").innerHTML = "";
      for (const ex of r.examples) {
        const d = document.createElement("div");
        d.className = "exrow";
        d.innerHTML = `<div class="thumbs">${ex.images.map((n) => `<img loading="lazy" src="/examples/${encodeURIComponent(n)}" alt="">`).join("")}</div>
          <div><h4>${esc(ex.title)}</h4><pre>${esc(ex.prompt)}</pre><div class="opts">Options: ${esc(ex.flags)}</div><div class="note">${esc(ex.note)}</div>
          <button>use this prompt</button></div>`;
        d.querySelector("button").onclick = () => { $("prompt").value = ex.prompt; $("prompt").focus(); };
        $("exlist").append(d);
      }
    } catch (e) { $("exlist").textContent = `couldn't load the examples: ${e.message}`; }
  };

  make.onclick = async () => {
    const [width, height] = $("shape").value.split("x").map(Number);
    make.disabled = true; state.className = ""; state.textContent = "sending…";
    try {
      await api.call({ action: "generate", prompt: $("prompt").value, negative: $("neg").value || null, width, height,
                       steps: Number($("steps").value), guidance: Number($("cfg").value), seed: $("seed").value === "" ? null : Number($("seed").value) });
    } catch (e) { state.textContent = e.message; state.className = "bad"; make.disabled = false; }
  };
  api.onEvent((ev) => {
    if (ev.type === "hello") load();
    if (ev.type === "image") load();  // she made one from the chat
    if (ev.type !== "panel" || ev.panel !== "images") return;
    const d = ev.data;
    if (d.progress) state.textContent = d.progress;
    if (d.failed) { state.textContent = `couldn't make it: ${d.failed}`; state.className = "bad"; make.disabled = false; }
    if (d.done) { state.textContent = `done in ${d.seconds}s (seed ${d.seed})`; make.disabled = false; load(); }
  });
  window.addEventListener("panel-shown", (e) => e.detail === "images" && load());
  load(); loadExamples();
}
