"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const { $, fetchJSON, renderPost, searchPage } = Archive;
  const page = searchPage({ endpoint: "/api/search", fields: ["q", "date_from", "date_to", "sort"], checkboxes: ["all_time"] });

  fetchJSON("/api/stats").then((s) => {
    $("stat-posts").textContent = s.total_posts.toLocaleString();
    $("stat-authors").textContent = s.tracked_authors.toLocaleString();
  }).catch(() => {});

  const [authors, channels] = await Promise.all([
    fetchJSON("/api/authors").catch(() => []),
    fetchJSON("/api/channels").catch(() => []),
  ]);
  page.addSelect("authors", authors);
  page.addSelect("channels", channels.map((c) => ({ id: c.id, name: (c.featured ? "* " : "") + c.name })));
  await page.run();

  // Live updates: new posts are prepended on the default first page; otherwise a banner offers a refresh.
  let pending = 0;
  const banner = $("live-banner");
  banner.addEventListener("click", () => { pending = 0; banner.hidden = true; page.run(); });

  function isDefaultView() {
    const p = page.params();
    p.delete("all_time");
    return page.currentPage() === 1 && !p.toString();
  }

  let source = null;
  function connect() {
    if (source || !$("live").checked || !window.EventSource) return;
    source = new EventSource("/stream");
    source.addEventListener("post", (e) => {
      const post = JSON.parse(e.data);
      if (document.querySelector(`.post[data-id="${post.id}"]`)) return;
      if (isDefaultView()) {
        const el = renderPost(post);
        el.classList.add("fresh");
        $("results").prepend(el);
      } else {
        pending += 1;
        banner.textContent = `${pending} new post${pending === 1 ? "" : "s"} - click to refresh`;
        banner.hidden = false;
      }
    });
  }
  $("live").addEventListener("change", () => {
    if ($("live").checked) connect();
    else if (source) { source.close(); source = null; }
  });
  connect();
});
