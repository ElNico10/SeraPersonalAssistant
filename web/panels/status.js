// title: Status
// How she is right now: mood, memory, reminders, coding jobs. Read-only; use the chat's !mood / !memory / !remind to change things.

// her status text is written for Discord: drop its bold markers
const esc = (s) => String(s).replace(/\*\*/g, "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);

export function mount(el, api) {
  el.innerHTML = `
    <style>
      #wrap { padding: 16px; display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); align-content: start; }
      .card { background: var(--panel); border: 1px solid var(--line); padding: 12px 14px; }
      .card h3 { margin: 0 0 8px; font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: var(--accent); }
      .card pre { margin: 0; white-space: pre-wrap; word-break: break-word; font: inherit; }
      #when { padding: 0 16px 16px; color: var(--dim); font-size: 12px; }
    </style>
    <div id="wrap"></div><div id="when"></div>`;
  const wrap = el.querySelector("#wrap"), when = el.querySelector("#when");
  const titles = { mood: "Mood", memory: "Memory", reminders: "Reminders", jobs: "Coding jobs", safety: "Picture safety", model: "Model" };

  const refresh = async () => {
    try {
      const s = await api.call({});
      wrap.innerHTML = Object.keys(titles).filter((k) => s[k]).map((k) => `<div class="card"><h3>${titles[k]}</h3><pre>${esc(s[k])}</pre></div>`).join("");
      when.textContent = `updated ${new Date().toLocaleTimeString()}`;
    } catch (e) { when.textContent = `couldn't read her status: ${e.message}`; }
  };
  window.addEventListener("panel-shown", (e) => e.detail === "status" && refresh());
  setInterval(() => el.classList.contains("active") && refresh(), 10000);
  refresh();
}
