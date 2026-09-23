// Live hot-sheet cards for an area page. <div id="area-listings" data-region="far east"
// data-lang="en">: shows that part of town's current listings from the same feed the
// home page uses. Fails quietly to the empty message: the page never depends on it.
(function () {
  var box = document.getElementById("area-listings");
  if (!box) return;
  var region = box.getAttribute("data-region") || "";
  var es = box.getAttribute("data-lang") === "es";
  var max = +(box.getAttribute("data-max") || 9);
  var API = "https://roberto-gavaldon3--ulises-realty-api-api.modal.run";
  var esc = function (s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  };
  var money = function (n) { return "$" + Math.round(n).toLocaleString("en-US"); };
  var empty = es
    ? "No hay casas nuevas en esta zona hoy. Llámeme o pregúnteme: busco en todo el MLS por usted."
    : "No new listings in this part of town today. Call or ask me and I'll search the full MLS for you.";
  fetch(API + "/listings-feed").then(function (r) { return r.json(); }).then(function (d) {
    var rows = (d.hot || []).filter(function (l) { return !region || l.region === region; }).slice(0, max);
    if (!rows.length) { box.innerHTML = '<div class="empty">' + empty + "</div>"; return; }
    box.className = "grid";
    box.innerHTML = rows.map(function (l) {
      var tag = es ? (l.hot_tag_es || l.hot_tag) : l.hot_tag;
      return '<a class="card" href="' + esc(l.url) + '" target="_blank" rel="noopener">' +
        (l.img ? '<img src="' + esc(l.img) + '" alt="' + esc(l.address) + '" loading="lazy">' : "") +
        '<div class="b"><div class="s">' + esc(tag || "") + '</div><div class="p">' + money(l.price) +
        '</div><div class="a">' + esc(l.address) + '</div><div class="s">' + esc(l.area) + " · " +
        (l.beds || "?") + (es ? " rec · " : " bd · ") + (l.baths || "?") + (es ? " baños" : " ba") + "</div>" +
        (l.office ? '<div class="c">' + (es ? "Cortesía de " : "Listing courtesy of ") + esc(l.office) + "</div>" : "") +
        "</div></a>";
    }).join("");
    var stamp = document.getElementById("area-updated");
    if (stamp && d.synced_at) {
      stamp.textContent = (es ? "Actualizada a diario · última actualización " : "Updated daily · last update ") +
        new Date(d.synced_at * 1000).toLocaleString(es ? "es-MX" : "en-US",
          {timeZone: "America/Denver", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
    }
  }).catch(function () { box.innerHTML = '<div class="empty">' + empty + "</div>"; });
})();
