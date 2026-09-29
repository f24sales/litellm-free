"use strict";
// HTMX handles transport/rendering. History is disabled; remove its startup marker.
try { sessionStorage.removeItem("htmx-current-path-for-history"); } catch (_) {}
function clearAnswers(scope = document) {
  for (const node of scope.querySelectorAll(".demo-answer")) {
    node.textContent = "";
    node.hidden = true;
  }
}
function setFireBusy(form, busy) {
  const button = form.querySelector(".fire-button");
  if (!button) return;
  if (busy) {
    button.setAttribute("aria-busy", "true");
    button.setAttribute("aria-label", "Test running");
  } else {
    button.removeAttribute("aria-busy");
    const state = button.dataset.state;
    button.setAttribute("aria-label", `Fire test${state && state !== "idle" ? `: ${state}` : ""}`);
  }
}
window.addEventListener("pagehide", () => clearAnswers());
document.addEventListener("htmx:beforeRequest", (event) => {
  const form = event.detail.elt.closest("form");
  if (form) clearAnswers(form);
});
// Route selection also uses HTMX; only the submitted Fire form gets a spinner.
for (const [name, busy] of [["htmx:beforeSend", true], ["htmx:afterRequest", false]]) {
  document.addEventListener(name, (event) => {
    const form = event.detail.elt;
    if (form.matches(".demo-form")) setFireBusy(form, busy);
  });
}
// Network failures also produce only a yellow button, never a message.
for (const name of ["htmx:sendError", "htmx:timeout", "htmx:responseError"]) {
  document.addEventListener(name, (event) => {
    const form = event.detail.elt.closest("form");
    if (form) {
      clearAnswers(form);
      form.querySelector("button").dataset.state = "yellow";
      setFireBusy(form, false);
    }
  });
}
