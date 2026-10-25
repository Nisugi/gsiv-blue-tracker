"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  const { fetchJSON, searchPage } = Archive;
  const page = searchPage({ endpoint: "/api/surprise_search", fields: ["q", "date_from", "date_to"], checkboxes: ["deleted", "all_time"] });
  const [members, channels] = await Promise.all([
    fetchJSON("/api/members").catch(() => []),
    fetchJSON("/api/channels").catch(() => []),
  ]);
  page.addSelect("authors", members.map((m) => ({ id: m.id, name: m.name + (m.is_tracked ? ` (${SITE.label})` : "") })));
  page.addSelect("channels", channels);
  await page.run();
});
