"use strict";
// Local, native dialogs; opening a guide never changes the model selection.
(() => {
  const dialogs = [...document.querySelectorAll(".aggregator-dialog")];
  const openers = new WeakMap();
  for (const dialog of dialogs) {
    if (typeof dialog.showModal !== "function") continue;
    dialog.classList.add("is-enhanced");
    const close = dialog.querySelector("[data-dialog-close]");
    close.hidden = false;
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right ||
          event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    });
    dialog.addEventListener("close", () => {
      if (window.location.hash === `#${dialog.id}`) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
      if (!document.querySelector(".aggregator-dialog[open]")) {
        document.documentElement.classList.remove("has-aggregator-dialog");
        openers.get(dialog)?.focus({ preventScroll: true });
      }
    });
  }
  const openFromHash = () => {
    const dialog = dialogs.find((item) => window.location.hash === `#${item.id}`);
    if (!dialog || typeof dialog.showModal !== "function") return;
    for (const other of dialogs) {
      if (other !== dialog && other.open) other.close();
    }
    if (!dialog.open) dialog.showModal();
    document.documentElement.classList.add("has-aggregator-dialog");
  };
  window.addEventListener("hashchange", openFromHash);
  openFromHash();
  for (const link of document.querySelectorAll("[data-aggregator-info]")) {
    link.addEventListener("click", (event) => {
      // Preserve native links for modified clicks and browsers without dialogs.
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const dialog = document.getElementById(link.dataset.aggregatorInfo);
      if (!dialog || typeof dialog.showModal !== "function") return;
      event.preventDefault();
      openers.set(dialog, link);
      dialog.showModal();
      document.documentElement.classList.add("has-aggregator-dialog");
    });
  }
})();
