// Shared rendering and search plumbing for the index and surprise pages.
"use strict";

const Archive = (() => {
  const $ = (id) => document.getElementById(id);
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function escapeHtml(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
  }

  // Minimal Discord-flavoured formatting on already-escaped text.
  function formatContent(text) {
    let html = escapeHtml(text);
    html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre>${code}</pre>`);
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    html = html.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/&lt;@!?(\d+)&gt;/g, '<span class="mention">@user</span>');
    html = html.replace(/&lt;#(\d+)&gt;/g, '<span class="mention">#channel</span>');
    return html.replace(/\n/g, "<br>");
  }

  function formatDate(iso) {
    return iso ? new Date(iso).toLocaleString() : "";
  }

  function renderAttachments(list) {
    if (!list || !list.length) return "";
    return '<div class="attachments">' + list.map((a) => {
      const url = escapeHtml(a.url);
      if ((a.content_type || "").startsWith("image/")) {
        return `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${escapeHtml(a.filename)}" loading="lazy"></a>`;
      }
      return `<a class="file" href="${url}" target="_blank" rel="noopener">${escapeHtml(a.filename || "attachment")}</a>`;
    }).join("") + "</div>";
  }

  function renderReply(r) {
    if (!r) return "";
    return `<a class="reply-context${r.is_tracked ? " tracked" : ""}" href="${escapeHtml(r.jump_url)}" target="_blank" rel="noopener">
      <span class="reply-author">${escapeHtml(r.author_name)}</span>
      <span class="reply-text">${escapeHtml((r.content || "").slice(0, 300))}</span></a>`;
  }

  function renderPost(p) {
    const el = document.createElement("article");
    el.className = "post" + (p.reason === "reply_target" ? " context-only" : "");
    el.dataset.id = p.id;
    const badges = [
      p.is_tracked ? `<span class="badge">${escapeHtml(SITE.label)}</span>` : "",
      p.reason === "reply_target" ? '<span class="badge muted">reply context</span>' : "",
      p.pinned ? '<span class="badge muted">pinned</span>' : "",
    ].join("");
    const edited = p.edit_count
      ? `<button type="button" class="link history">edited ${p.edit_count}x</button>`
      : (p.edited_at ? '<span class="muted">(edited)</span>' : "");
    el.innerHTML = `
      ${renderReply(p.replied_to)}
      <div class="post-header">
        <span class="author">${escapeHtml(p.author_name)}</span>${badges}
        <span class="channel">#${escapeHtml(p.channel)}</span>
        <span class="time" title="${escapeHtml(p.created_at)}">${escapeHtml(formatDate(p.created_at))}</span>
        ${edited}
        <a class="jump" href="${escapeHtml(p.jump_url)}" target="_blank" rel="noopener">Jump to message</a>
      </div>
      <div class="content">${formatContent(p.content)}</div>
      ${renderAttachments(p.attachments)}
      <div class="revisions" hidden></div>`;
    const btn = el.querySelector(".history");
    if (btn) btn.addEventListener("click", () => toggleRevisions(el, p.id));
    return el;
  }

  async function toggleRevisions(el, id) {
    const box = el.querySelector(".revisions");
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    box.textContent = "Loading history...";
    try {
      const revs = await fetchJSON(`/api/posts/${id}/revisions`);
      box.innerHTML = revs.length ? revs.map((r) => `
        <div class="revision"><div class="muted">Before edit, captured ${escapeHtml(formatDate(r.captured_at))}</div>
        <div class="content">${formatContent(r.content || "")}</div></div>`).join("") : "No earlier versions stored.";
    } catch (e) {
      box.textContent = "Could not load history.";
    }
  }

  async function fetchJSON(url) {
    const res = await fetch(url);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  }

  function setupSelect(id, items) {
    const select = $(id);
    select.innerHTML = items.map((i) => `<option value="${escapeHtml(i.id)}">${escapeHtml(i.name)}</option>`).join("");
    if (window.Choices) {
      return new Choices(select, { removeItemButton: true, shouldSort: false, searchResultLimit: 50,
        placeholderValue: select.dataset.placeholder, itemSelectText: "" });
    }
    return null;
  }

  function selectedValues(id) {
    return Array.from($(id).selectedOptions).map((o) => o.value);
  }

  function renderPager(data, onPage) {
    const pager = $("pager");
    pager.innerHTML = "";
    if (data.total_pages <= 1) return;
    const add = (label, page, disabled) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label; b.disabled = disabled;
      b.addEventListener("click", () => onPage(page));
      pager.appendChild(b);
    };
    add("First", 1, data.page === 1);
    add("Prev", data.page - 1, data.page === 1);
    const span = document.createElement("span");
    span.textContent = `Page ${data.page} of ${data.total_pages}`;
    pager.appendChild(span);
    add("Next", data.page + 1, data.page >= data.total_pages);
    add("Last", data.total_pages, data.page >= data.total_pages);
  }

  function renderResults(data) {
    const results = $("results");
    results.innerHTML = "";
    data.results.forEach((p) => results.appendChild(renderPost(p)));
    let info = `${data.total.toLocaleString()}${data.capped ? "+" : ""} result${data.total === 1 ? "" : "s"}`;
    if (data.capped) info += " (showing the first 10,000; narrow your search)";
    if (data.limited_to_recent) info += " - last 90 days only; tick All time for more";
    $("info").textContent = info;
  }

  // Wires a search form to an endpoint; keeps the state in the URL so searches can be shared.
  function searchPage({ endpoint, fields, checkboxes }) {
    const state = new URLSearchParams(location.search);
    const selects = {};
    let page = parseInt(state.get("page") || "1", 10);

    fields.forEach((f) => { if (state.get(f)) $(f).value = state.get(f); });
    checkboxes.forEach((c) => { $(c).checked = state.get(c) === "1"; });

    function params() {
      const p = new URLSearchParams();
      fields.forEach((f) => {
        const v = $(f).value.trim();
        if (v && !(f === "sort" && v === "desc")) p.set(f, v);
      });
      checkboxes.forEach((c) => { if ($(c).checked) p.set(c, "1"); });
      ["authors", "channels"].forEach((s) => { const v = selectedValues(s); if (v.length) p.set(s, v.join(",")); });
      if (page > 1) p.set("page", page);
      return p;
    }

    async function run() {
      const p = params();
      history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
      $("info").textContent = "Searching...";
      try {
        const data = await fetchJSON(`${endpoint}?${p}`);
        renderResults(data);
        renderPager(data, (n) => { page = n; run(); window.scrollTo(0, 0); });
      } catch (e) {
        $("info").textContent = `Search failed: ${e.message}`;
      }
    }

    $("search-form").addEventListener("submit", (e) => { e.preventDefault(); page = 1; run(); });
    $("reset").addEventListener("click", () => {
      fields.forEach((f) => { $(f).value = f === "sort" ? "desc" : ""; });
      checkboxes.forEach((c) => { $(c).checked = false; });
      Object.entries(selects).forEach(([id, choices]) => {
        if (choices) choices.removeActiveItems();
        else Array.from($(id).options).forEach((o) => { o.selected = false; });
      });
      page = 1; run();
    });

    return {
      run,
      params,
      currentPage: () => page,
      addSelect(id, items) {
        const wanted = (state.get(id) || "").split(",").filter(Boolean);
        selects[id] = setupSelect(id, items);
        if (!wanted.length) return;
        if (selects[id]) selects[id].setChoiceByValue(wanted);
        else Array.from($(id).options).forEach((o) => { o.selected = wanted.includes(o.value); });
      },
    };
  }

  return { $, escapeHtml, formatContent, renderPost, fetchJSON, searchPage };
})();
