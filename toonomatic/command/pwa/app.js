// Toon-o-Matic COMMAND — la télécommande du studio. Aucune vidéo n'est téléchargée :
// tout est lu en streaming depuis le serveur / le vault.
const $app = document.getElementById("app");
const TIER = { draft: "Brouillon", validation: "Validation", final: "Final" };
const STATUS = { queued: "En file", running: "En production", review: "À valider", approved: "Validé",
  published: "Publié", failed: "Échec", rejected: "Rejeté" };
const CRIT = ["histoire", "personnages", "animation", "son", "publication"];
let CFG = null, timer = null;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function toast(msg) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 3500);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { renderLogin(); throw new Error("auth"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Erreur ${r.status}`);
  return data;
}
const badge = (cls, txt) => `<span class="badge ${cls}">${esc(txt)}</span>`;
const stBadge = s => badge("b-" + s, STATUS[s] || s);
const tierBadge = t => t ? badge("t-" + t, TIER[t] || t) : "";
const scoreColor = v => v >= 7 ? "var(--ok)" : v >= 5 ? "var(--warn)" : "var(--bad)";

function renderLogin() {
  clearInterval(timer);
  $app.innerHTML = `<div class="card"><h1>Connexion au studio</h1>
    <p class="muted small">Entrez le jeton défini sur le serveur (TOONOMATIC_TOKEN).</p>
    <label for="tok">Jeton</label><input id="tok" type="password" autocomplete="current-password">
    <button class="btn primary block" id="go">Se connecter</button></div>`;
  document.getElementById("go").onclick = async () => {
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ token: document.getElementById("tok").value }) });
      route();
    } catch (e) { if (e.message !== "auth") toast(e.message); }
  };
}

async function renderList() {
  const eps = await api("/api/episodes");
  $app.innerHTML = `<h1>${esc(CFG.series?.fr || "Studio")}</h1>` + (eps.length ? eps.map(e => `
    <a class="card" href="#/ep/${encodeURIComponent(e.id)}">
      <div class="row">${stBadge(e.status)} ${tierBadge(e.tier)}</div>
      <div style="margin-top:6px;font-weight:600">${esc(e.title)}</div>
      <div class="muted small">${esc(e.id)}${e.error ? " · ⚠️ " + esc(e.error.slice(0, 80)) : ""}</div>
    </a>`).join("") : `<div class="card"><p>Aucun épisode pour l'instant.</p>
      <a class="btn primary block" href="#/new">Commander un premier épisode</a></div>`);
  if (eps.some(e => ["queued", "running"].includes(e.status))) timer = setTimeout(route, 8000);
}

async function renderNew() {
  const sits = await api("/api/situations");
  const tiers = CFG.tiers.map(t => `<option value="${t}" ${t === "draft" ? "selected" : ""}>${TIER[t] || t}${CFG.gpu_tiers.includes(t) ? " (GPU)" : " (CPU, économique)"}</option>`).join("");
  $app.innerHTML = `<div class="card"><h1>Nouvel épisode</h1>
    <label for="sit">Situation de départ</label>
    <select id="sit"><option value="">🎲 Au hasard (banque d'idées)</option>${sits.map(s => `<option value="${esc(s.id)}">${esc(s.situation.slice(0, 70))}</option>`).join("")}
      <option value="__free">✍️ Écrire ma propre idée…</option></select>
    <div id="freeBox" hidden><label for="free">Votre idée</label><textarea id="free" rows="4" placeholder="Ex. : le tonton vend la même voiture à deux voisins le même jour."></textarea></div>
    <label for="tier">Niveau de production</label><select id="tier">${tiers}</select>
    <p class="muted small">Conseil : commencez en Brouillon. On ne paie le rendu final qu'une fois l'histoire validée.</p>
    <button class="btn primary block" id="go">Lancer la production</button></div>`;
  const sel = document.getElementById("sit");
  sel.onchange = () => document.getElementById("freeBox").hidden = sel.value !== "__free";
  document.getElementById("go").onclick = async ev => {
    ev.target.disabled = true;
    const body = { tier: document.getElementById("tier").value };
    if (sel.value === "__free") body.situation_text = document.getElementById("free").value;
    else if (sel.value) body.situation_id = sel.value;
    try { const r = await api("/api/orders", { method: "POST", body: JSON.stringify(body) });
      toast("Commande envoyée au studio"); location.hash = "#/ep/" + encodeURIComponent(r.episode_id);
    } catch (e) { toast(e.message); ev.target.disabled = false; }
  };
}

function scoresHtml(rep) {
  if (!rep?.scores) return "";
  const rows = CRIT.map(c => { const v = rep.scores[c] ?? 0; return `<div>${c}</div>
    <div class="meter"><i style="width:${v * 10}%;background:${scoreColor(v)}"></i></div><div>${v}</div>`; }).join("");
  const notes = Object.entries(rep.languages || {}).map(([l, x]) => x.notes ? `<p class="small"><b>${l.toUpperCase()}</b> · ${esc(x.notes)}</p>` : "").join("");
  const fixes = (rep.fixes || []).map(f => `<div class="fix small">${esc(f.action)} · ${esc(f.lang)} ${f.line !== undefined ? "#" + f.line : esc(f.scene || "")} — ${esc(f.why || "")}</div>`).join("");
  const unres = (rep.unresolved || []).length ? `<p class="small"><b>⚠️ Non résolu automatiquement — à vous de juger :</b></p>` +
    rep.unresolved.map(f => `<div class="fix small">${esc(f.action)} · ${esc(f.lang)} ${f.line !== undefined ? "#" + f.line : esc(f.scene || "")} — ${esc(f.why || "")}</div>`).join("") : "";
  return `<h2>Réalisateur automatique · ${esc(rep.verdict || "")}</h2><div class="card"><div class="scores">${rows}</div>${notes}${unres || fixes}</div>`;
}

async function renderEpisode(id) {
  const ep = await api("/api/episodes/" + encodeURIComponent(id));
  const langs = [...new Set(ep.media.map(m => m.lang))];
  const busy = ["queued", "running"].includes(ep.status);
  const videos = langs.map(l => ep.media.filter(m => m.lang === l).map(m =>
    `<div class="card ${m.kind === "short" ? "vertical" : ""}" data-lang="${l}"><div class="small muted">${l.toUpperCase()} · ${m.kind === "short" ? "extrait vertical" : "épisode"}</div>
     <div style="font-weight:600;margin:4px 0">${esc(m.title)}</div>
     <video controls playsinline preload="metadata" src="${m.src}"></video></div>`).join("")).join("");
  const next = ep.next_tier;
  const actions = busy ? `<p class="muted">Le studio travaille… cette page se met à jour toute seule.</p>` : ep.status === "review" ? `
    <h2>Décision</h2><div class="card">
      ${next ? `<button class="btn ok block" data-d="promote">✅ Valider le ${TIER[ep.tier].toLowerCase()} → passer en ${TIER[next]}${CFG.gpu_tiers.includes(next) ? " (GPU)" : ""}</button>`
             : `<button class="btn ok block" data-d="approve">✅ Valider et publier</button>`}
      <label for="reason">Correction demandée (devient une leçon pour la série)</label>
      <textarea id="reason" rows="3" placeholder="Ex. : la chute de la scène 3 tombe à plat, Lina est trop gentille."></textarea>
      <label for="from">Refaire à partir de</label>
      <select id="from"><option value="script">les dialogues</option><option value="premise">la structure</option>
        <option value="voice">les voix</option><option value="render">l'animation</option></select>
      <button class="btn block" data-d="redo">🔁 Corriger</button>
      <button class="btn bad block" data-d="reject">⛔ Rejeter</button></div>` : "";
  const events = (ep.events || []).slice(-8).reverse().map(e =>
    `<div class="small muted">${new Date(e.ts * 1000).toLocaleTimeString("fr-FR")} · ${esc(e.kind)} ${esc(e.payload?.stage || e.payload?.action || e.payload?.decision || "")}</div>`).join("");
  $app.innerHTML = `<a href="#/" class="back small">← Épisodes</a>
    <h1>${esc((ep.info.deliverables || [])[0]?.title || ep.id)}</h1>
    <div class="row">${stBadge(ep.status)} ${tierBadge(ep.tier)} <span class="small muted">risque ${ep.risk ?? "–"}</span></div>
    ${ep.info.error ? `<div class="card fix">${esc(ep.info.error)}</div>` : ""}
    ${langs.length > 1 ? `<div class="tabs">${langs.map(l => `<button class="btn small" data-l="${l}">${l.toUpperCase()}</button>`).join("")}</div>` : ""}
    ${videos || (busy ? "" : '<p class="muted">Pas encore de vidéo.</p>')}
    ${scoresHtml(ep.director_report)}${actions}
    <h2>Journal</h2><div class="card">${events || '<span class="muted small">—</span>'}</div>`;
  document.querySelectorAll("[data-l]").forEach(b => b.onclick = () => {
    document.querySelectorAll("[data-l]").forEach(x => x.classList.toggle("active", x === b));
    document.querySelectorAll("[data-lang]").forEach(c => c.hidden = c.dataset.lang !== b.dataset.l);
  });
  if (langs.length > 1) document.querySelector("[data-l]").click();
  document.querySelectorAll("[data-d]").forEach(b => b.onclick = async () => {
    const d = b.dataset.d;
    const reason = document.getElementById("reason")?.value || "";
    if (d === "reject" && !confirm("Rejeter définitivement cet épisode ?")) return;
    if (d === "redo" && !reason.trim()) { toast("Expliquez ce qu'il faut corriger"); return; }
    b.disabled = true;
    try { await api(`/api/episodes/${encodeURIComponent(id)}/decision`, { method: "POST",
        body: JSON.stringify({ decision: d, reason, from: document.getElementById("from")?.value }) });
      toast("Décision envoyée"); route();
    } catch (e) { toast(e.message); b.disabled = false; }
  });
  if (busy) timer = setTimeout(route, 6000);
}

async function route() {
  clearTimeout(timer);
  try {
    if (!CFG) CFG = await api("/api/config");
    const h = location.hash.slice(1) || "/";
    if (h.startsWith("/ep/")) await renderEpisode(decodeURIComponent(h.slice(4)));
    else if (h === "/new") await renderNew();
    else await renderList();
  } catch (e) { if (e.message !== "auth") $app.innerHTML = `<div class="card fix">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
route();
