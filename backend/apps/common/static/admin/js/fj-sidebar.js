/* Collapse toggle for the Voltagent admin sidebar.
   Mirrors web/app.html: icon-only below 900px, state kept per browser. */
(function () {
  "use strict";
  var KEY = "fj-admin-sidebar";
  var sidebar = document.getElementById("fj-sidebar");
  var toggle = document.getElementById("fj-sidebar-toggle");
  if (!sidebar || !toggle) return;

  var stored = null;
  try { stored = window.localStorage.getItem(KEY); } catch (e) { /* private mode */ }
  var collapsed = stored === null ? window.innerWidth < 900 : stored === "1";

  function apply() {
    sidebar.classList.toggle("fj-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  apply();

  toggle.addEventListener("click", function () {
    collapsed = !collapsed;
    try { window.localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
    apply();
  });
})();
