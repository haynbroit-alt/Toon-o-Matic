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
let HEALTH = null;
function banner() {
  const w = HEALTH?.warnings || [];
  return w.length ? `<div class="card fix"><b>Installation à compléter</b>${w.map(x => `<p class="small">⚠️ ${esc(x)}</p>`).join("")}</div>` : "";
}
function renderSetup(warnings) {
  $app.innerHTML = `<div class="card"><h1>Studio verrouillé</h1>
    <p>Le serveur fonctionne, mais il n'accepte aucune commande tant qu'il n'est pas sécurisé.</p>
    ${warnings.map(x => `<p class="small">⚠️ ${esc(x)}</p>`).join("")}
    <button class="btn block" onclick="location.reload()">J'ai corrigé, réessayer</button></div>`;
}
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 503) { const d = await r.json().catch(() => ({})); renderSetup(d.warnings || []); throw new Error("auth"); }
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
  $app.innerHTML = banner() + `<h1>${esc(CFG.series?.fr || "Studio")}</h1>` + (eps.length ? eps.map(e => `
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
    <button class="btn primary block" id="go">Lancer la production</button>
    <p class="muted small" style="margin-top:14px">Pas de clé de modèle, ou envie d'une autre plume ?</p>
    <a class="btn block" href="#/desk" style="text-align:center">✍️ Bureau d'écriture (copier-coller, sans clé)</a></div>`;
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
  const rows = CRIT.map(c => { const v = rep.scores[c]; return v == null
    ? `<div>${c}</div><div class="small muted">non noté</div><div>–</div>`
    : `<div>${c}</div><div class="meter"><i style="width:${v * 10}%;background:${scoreColor(v)}"></i></div><div>${v}</div>`; }).join("");
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
    ${ep.info.written_by === "bureau" ? `<div class="small muted">✍️ Scénario du bureau d'écriture${(ep.info.desk_warnings || []).map(w => " · " + esc(w)).join("")}</div>` : ""}
    ${langs.length > 1 ? `<div class="tabs">${langs.map(l => `<button class="btn small" data-l="${l}">${l.toUpperCase()}</button>`).join("")}</div>` : ""}
    ${videos || (busy ? "" : '<p class="muted">Pas encore de vidéo.</p>')}
    ${presenceHtml(ep)}${scoresHtml(ep.director_report)}${actions}
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

// ---- Bureau d'écriture : le scénario s'écrit dans l'application de chat de votre choix
const DESK_KEY = "tm_desk";
const deskLoad = () => { try { return JSON.parse(localStorage.getItem(DESK_KEY) || "null"); } catch { return null; } };
const deskSave = d => { try { d ? localStorage.setItem(DESK_KEY, JSON.stringify(d)) : localStorage.removeItem(DESK_KEY); } catch {} };
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; } catch {
    const ta = document.getElementById("brief"); if (!ta) return false;
    ta.focus(); ta.select(); try { return document.execCommand("copy"); } catch { return false; }
  }
}
async function renderDesk() {
  const d = deskLoad();   // gardé sur le téléphone : on peut quitter l'appli pour aller dans le chat et revenir
  if (!d) {
    const sits = await api("/api/situations");
    $app.innerHTML = `<a href="#/new" class="back small">← Nouvel épisode</a><div class="card"><h1>Bureau d'écriture</h1>
      <p class="small muted">Le scénario est écrit par l'application de chat de votre choix (Claude, Le Chat, ChatGPT, Gemini…). Aucune clé n'est nécessaire : le studio prépare le brief, vous collez la réponse.</p>
      <label for="sit">Situation de départ</label>
      <select id="sit"><option value="">🎲 Au hasard (banque d'idées)</option>${sits.map(s => `<option value="${esc(s.id)}">${esc(s.situation.slice(0, 70))}</option>`).join("")}
        <option value="__free">✍️ Écrire ma propre idée…</option></select>
      <div id="freeBox" hidden><label for="free">Votre idée</label><textarea id="free" rows="4"></textarea></div>
      <button class="btn primary block" id="mk">1 · Préparer le brief</button></div>`;
    const sel = document.getElementById("sit");
    sel.onchange = () => document.getElementById("freeBox").hidden = sel.value !== "__free";
    document.getElementById("mk").onclick = async ev => {
      ev.target.disabled = true;
      const body = sel.value === "__free" ? { situation_text: document.getElementById("free").value } : (sel.value ? { situation_id: sel.value } : {});
      try { const r = await api("/api/desk/brief", { method: "POST", body: JSON.stringify(body) });
        deskSave({ situation: r.situation, brief: r.brief, plan: r.plan, answer: "" }); renderDesk();
      } catch (e) { if (e.message !== "auth") toast(e.message); ev.target.disabled = false; }
    };
    return;
  }
  const tiers = CFG.tiers.map(t => `<option value="${t}" ${t === "draft" ? "selected" : ""}>${TIER[t] || t}</option>`).join("");
  $app.innerHTML = `<a href="#/new" class="back small">← Nouvel épisode</a>
    <div class="card"><h1>Bureau d'écriture</h1>
      <p class="small"><b>Situation :</b> ${esc(d.situation.situation)}</p>
      <p class="small muted">${d.plan.scenes} scènes · ${d.plan.minutes} min · personnages : ${esc(d.plan.cast.join(", "))}</p></div>
    <div class="card"><h2 style="margin-top:0">1 · Copiez le brief</h2>
      <textarea id="brief" rows="5" readonly>${esc(d.brief)}</textarea>
      <button class="btn primary block" id="cp">📋 Copier le brief</button></div>
    <div class="card"><h2 style="margin-top:0">2 · Collez-le dans votre application de chat</h2>
      <p class="small muted">Ouvrez une nouvelle conversation, collez, envoyez. Copiez ensuite TOUTE la réponse (le bloc de code). Si elle est coupée, demandez : « renvoie le JSON complet ».</p></div>
    <div class="card"><h2 style="margin-top:0">3 · Collez la réponse ici</h2>
      <textarea id="ans" rows="7" placeholder='{ "logline": … }'>${esc(d.answer || "")}</textarea>
      <div id="probs"></div>
      <label for="tier">Niveau de production</label><select id="tier">${tiers}</select>
      <button class="btn ok block" id="imp">Vérifier et lancer la production</button>
      <button class="btn block" id="rst">Recommencer avec une autre situation</button></div>`;
  const ans = document.getElementById("ans");
  ans.oninput = () => deskSave({ ...d, answer: ans.value });
  document.getElementById("cp").onclick = async () => toast(await copyText(d.brief) ? "Brief copié" : "Copie impossible : sélectionnez le texte à la main");
  document.getElementById("rst").onclick = () => { deskSave(null); renderDesk(); };
  document.getElementById("imp").onclick = async ev => {
    ev.target.disabled = true;
    const r = await fetch("/api/desk/import", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ situation: d.situation, text: ans.value, tier: document.getElementById("tier").value }) });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) { renderLogin(); return; }
    if (r.status === 422) {
      document.getElementById("probs").innerHTML = `<div class="fix small"><b>À corriger avant de lancer :</b>${data.problems.map(x => `<div>• ${esc(x)}</div>`).join("")}</div>`;
      ev.target.disabled = false; return;
    }
    if (!r.ok) { toast(data.error || `Erreur ${r.status}`); ev.target.disabled = false; return; }
    deskSave(null);
    toast(data.warnings?.length ? "Lancé · " + data.warnings[0] : "Scénario accepté, production lancée");
    location.hash = "#/ep/" + encodeURIComponent(data.episode_id);
  };
}

const KIND = { scene: "Scène", reaction: "Réaction 10 s", short: "Short" };
function presenceHtml(ep) {
  if (!ep.presence?.length) return "";
  const byScene = {};
  ep.presence.forEach(a => (byScene[a.scene + "|" + a.lang] ||= []).push(a));
  return `<h2>Présence · une scène, plusieurs rencontres</h2>` + Object.entries(byScene).map(([k, as]) => {
    const [scene, lang] = k.split("|");
    return `<div class="card" data-lang="${lang}"><div class="small muted">${scene} · ${lang.toUpperCase()} · ${esc(as[0].summary || "")}</div>
      <div class="grid2">${as.map(a => `<div><div class="small"><b>${KIND[a.kind] || a.kind}</b> · ${a.duration} s</div>
        <video controls playsinline preload="none" src="${a.src}"></video>
        <div class="small muted">${esc(a.caption)}</div><div class="small muted">id : ${esc(a.id)}</div></div>`).join("")}</div></div>`;
  }).join("");
}

const loc = (v, l = "fr") => (v && typeof v === "object") ? (v[l] || Object.values(v)[0] || "") : (v || "");
async function renderCast() {
  const cast = await api("/api/characters");
  $app.innerHTML = `<h1>Personnages</h1>` + cast.map(c => {
    const k = c.kernel || {}, sg = c.signature || {}, so = c.social || {};
    const eng = so.engagement, men = so.mentions;
    const quotes = (so.quoted_lines || []).map(q => `<div class="small">« ${esc(q.line)} » · ${q.quotes} reprise(s) <span class="prov">mesuré</span></div>`).join("");
    const exprs = (so.best_expressions || []).map(e => `<div class="small">${esc(e.expression[1])} / ${esc(e.expression[2])} · n=${e.n} <span class="prov">${e.sufficient ? "mesuré" : "insuffisant"}</span></div>`).join("");
    const hist = (c.history || []).slice(-3).reverse().map(h => `<div class="small muted">${esc(h.episode)} ${esc(h.scene)} · ${esc((h.summary || "").slice(0, 70))}</div>`).join("");
    return `<div class="card"><h2 style="margin-top:0"><span class="swatch" style="background:${esc(sg.outfit_color || "#999")}"></span>${esc(c.display)}</h2>
      <div class="kv"><b>Archétype</b><span>${esc(loc(k.archetype))}</span><b>Désir</b><span>${esc(loc(k.desire))}</span>
        <b>Peur</b><span>${esc(loc(k.fear))}</span><b>Contradiction</b><span>${esc(loc(k.contradiction))}</span>
        <b>Gimmick</b><span>${esc(loc(k.gimmick))}</span><b>Silhouette</b><span>${esc(sg.build?.shape || "–")}</span></div>
      <h2>Ce que le public en fait</h2>
      ${eng ? `<div class="small">Engagement ${eng.engagement} · n=${eng.n} <span class="prov">${eng.sufficient ? "mesuré" : "insuffisant"}</span></div>` : `<div class="small muted">Pas encore de données mesurées.</div>`}
      ${men ? `<div class="small">Cité dans ${men.comments} commentaire(s) public(s) <span class="prov">mesuré</span></div>` : ""}
      ${quotes}${exprs}<div class="small muted">Partages WhatsApp privés : non mesurables.</div>
      <h2>Histoire vécue · ${c.scenes_lived} scène(s)</h2>${hist || '<div class="small muted">—</div>'}</div>`;
  }).join("");
}

async function route() {
  clearTimeout(timer);
  try {
    if (!HEALTH) HEALTH = await fetch("/api/health").then(r => r.json()).catch(() => null);
    if (HEALTH?.locked) { renderSetup(HEALTH.warnings); return; }
    if (!CFG) CFG = await api("/api/config");
    const h = location.hash.slice(1) || "/";
    if (h.startsWith("/ep/")) await renderEpisode(decodeURIComponent(h.slice(4)));
    else if (h === "/cast") await renderCast();
    else if (h === "/desk") await renderDesk();
    else if (h === "/new") await renderNew();
    else await renderList();
  } catch (e) { if (e.message !== "auth") $app.innerHTML = `<div class="card fix">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
route();
