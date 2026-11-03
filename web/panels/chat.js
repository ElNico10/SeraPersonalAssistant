// title: Chat
// Talk to Sera. Everything you type reaches her as a message from you, so her commands (!mood, !memory, !remind) work too.

const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);

// A little formatting: ``` blocks, `code`, **bold**. Everything is escaped first, so nothing she writes can inject HTML.
function format(text) {
  const parts = esc(text).split(/```(?:\w*\n)?/);
  return parts.map((p, i) => i % 2
    ? `<pre>${p.replace(/\n$/, "")}</pre>`
    : p.replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>")).join("");
}

export function mount(el, api) {
  el.innerHTML = `
    <style>
      #log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
      .row { max-width: 900px; word-break: break-word; }
      .row .who { color: var(--dim); margin-right: 8px; }
      .row.you .who { color: var(--you); }
      .row.sera .who { color: var(--accent); }
      .row.err { color: var(--warn); }
      .row img { display: block; max-width: min(100%, 520px); margin-top: 6px; border: 1px solid var(--line); }
      .row pre { background: var(--panel); border: 1px solid var(--line); padding: 10px; overflow-x: auto; margin: 6px 0; }
      .row code { background: var(--panel); padding: 1px 5px; border-radius: 3px; }
      #typing { height: 20px; padding: 0 16px; color: var(--dim); font-size: 12px; }
      #bar { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--line); background: var(--panel); }
      #box { flex: 1; resize: none; background: var(--bg); color: var(--text); border: 1px solid var(--line); padding: 8px 10px; font: inherit; max-height: 160px; }
      #box:focus { outline: 1px solid var(--accent); }
      #go { background: var(--accent); color: var(--bg); border: 0; padding: 0 16px; font: inherit; font-weight: bold; cursor: pointer; }
    </style>
    <div id="log"></div><div id="typing"></div>
    <div id="bar"><textarea id="box" rows="1" placeholder="say something… (Enter sends, Shift+Enter for a new line)"></textarea><button id="go">send</button></div>`;
  const log = el.querySelector("#log"), box = el.querySelector("#box"), typing = el.querySelector("#typing");
  const sent = []; let at = 0;

  const add = (cls, who, text) => {
    const d = document.createElement("div");
    d.className = `row ${cls}`;
    d.innerHTML = `<span class="who">${esc(who)}›</span>${format(text)}`;
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 80;
    log.append(d);
    if (stick) log.scrollTop = log.scrollHeight;
  };
  const show = (ev) => {
    if (ev.type === "say") add("sera", "sera", ev.text);
    else if (ev.type === "user") add("you", api.owner(), ev.text);
    else if (ev.type === "error") add("err", "!", ev.text);
    else if (ev.type === "image") {
      add("sera", "sera", "");
      const last = log.lastElementChild;
      last.insertAdjacentHTML("beforeend", `<a href="${ev.url}" target="_blank"><img src="${ev.url}" alt="picture"></a>`);
    }
  };

  api.onEvent((ev) => {
    if (ev.type === "hello") { log.innerHTML = ""; ev.backlog.forEach(show); log.scrollTop = log.scrollHeight; }
    else if (ev.type === "typing") typing.textContent = ev.on ? "sera is typing…" : "";
    else show(ev);
  });

  const send = () => {
    const text = box.value.trim();
    if (!text) return;
    api.say(text); sent.push(text); at = sent.length;
    box.value = ""; box.style.height = "auto";
  };
  el.querySelector("#go").onclick = send;
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    else if (e.key === "ArrowUp" && !box.value.includes("\n") && at > 0) { box.value = sent[--at]; e.preventDefault(); }
    else if (e.key === "ArrowDown" && at < sent.length) { box.value = sent[++at] ?? ""; e.preventDefault(); }
  });
  box.addEventListener("input", () => { box.style.height = "auto"; box.style.height = box.scrollHeight + "px"; });
  window.addEventListener("panel-shown", (e) => e.detail === "chat" && box.focus());
  box.focus();
}
