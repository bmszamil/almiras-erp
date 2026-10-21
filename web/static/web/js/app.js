/* Progressive enhancement only (web.md W-D2, R20): without JavaScript the navigation is simply shown open.
 * With it, narrow screens get a Menu button that opens and closes the navigation panel. No inline handlers. */
(function () {
  "use strict";
  document.documentElement.classList.add("alm-js");
  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-alm-nav-toggle]");
    var panel = document.getElementById("alm-nav");
    if (!button || !panel) {
      return;
    }
    button.hidden = false;
    button.addEventListener("click", function () {
      var open = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", open ? "false" : "true");
      panel.classList.toggle("is-open", !open);
    });
  });
})();
