/* Form helpers (progressive enhancement: every form works without JavaScript).
   1. Destructive actions ask for confirmation: <form data-confirm="Delete this?">.
   2. When a form comes back with errors, focus moves to the error summary so
      keyboard and screen-reader users hear what to fix first. */
(function () {
  "use strict";

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) ev.preventDefault();
  }, true);

  document.addEventListener("DOMContentLoaded", function () {
    var summary = document.querySelector(".error-summary");
    if (summary) {
      summary.setAttribute("tabindex", "-1");
      summary.focus();
    }
  });
})();
