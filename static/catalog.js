"use strict";
// Presentation only: no API calls, saved preferences, cookies or catalog mutation.
(() => {
  const choices = [...document.querySelectorAll(".tile-choice")];
  const cards = [...document.querySelectorAll(".model-card")];
  const reset = document.getElementById("reset-choices");
  const status = document.getElementById("selection-status");
  const empty = document.getElementById("selection-empty");
  if (!reset || !status || !empty) return;
  const groups = ["aggregator", "provider"];
  const records = cards.map((card) => ({
    card, provider: [card.dataset.provider], aggregator: card.dataset.aggregators.split(/\s+/)
  }));
  const all = Object.fromEntries(groups.map((kind) => [kind,
    new Set(records.flatMap((record) => record[kind]))]));
  const selected = { aggregator: new Set(all.aggregator), provider: new Set(all.provider) };
  let allSelected = true;
  const finishReveal = (target) => target.classList.remove(
    "is-revealed", "reveal-step-0", "reveal-step-1", "reveal-step-2");
  function neighbours(kind, values) {
    const other = kind === "provider" ? "aggregator" : "provider";
    return new Set(records.filter((record) => record[kind].some((value) => values.has(value)))
      .flatMap((record) => record[other]));
  }
  function filter() {
    allSelected = choices.every((button) => selected[button.dataset.filter].has(button.dataset.value));
    // Once every visible tile is selected, anonymous models are included too.
    if (allSelected) groups.forEach((kind) => { selected[kind] = new Set(all[kind]); });
    choices.forEach((button) => button.setAttribute("aria-pressed",
      String(selected[button.dataset.filter].has(button.dataset.value))));
    let count = 0;
    for (const record of records) {
      const hidden = !groups.every((kind) => record[kind].some((value) => selected[kind].has(value)));
      // display:none cancels CSS animations; discard them so a later selection
      // does not replay the assembly on a card already seen by the visitor.
      if (hidden) finishReveal(record.card);
      record.card.hidden = hidden;
      if (!record.card.hidden) count++;
    }
    reset.hidden = allSelected;
    empty.hidden = count !== 0 || records.length === 0;
    status.textContent = `${count} ${count === 1 ? "model" : "models"} shown`;
  }
  for (const button of choices) {
    button.disabled = false;
    button.addEventListener("click", () => {
      const { filter: kind, value } = button.dataset;
      const other = kind === "provider" ? "aggregator" : "provider";
      if (allSelected) {
        selected[kind] = new Set([value]);
        selected[other] = neighbours(kind, selected[kind]);
      } else if (selected[kind].has(value)) {
        selected[kind].delete(value);
        const compatible = neighbours(kind, selected[kind]);
        selected[other] = new Set([...selected[other]].filter((item) => compatible.has(item)));
      } else {
        selected[kind].add(value);
        neighbours(kind, new Set([value])).forEach((item) => selected[other].add(item));
      }
      filter();
    });
  }
  reset.addEventListener("click", () => {
    groups.forEach((kind) => { selected[kind] = new Set(all[kind]); });
    filter();
    choices[0]?.focus();
  });
  filter();

  // Progressive enhancement: content is visible even without this observer.
  // Animate each item once on entry; never replace or hijack native scrolling.
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (motion.matches || !("IntersectionObserver" in window)) return;
  const targets = [...document.querySelectorAll(".masthead .wordmark, .hero-copy, .brand-rhythm, .gateway-counts > li, .provider-tiles > li, .model-card")];
  const observer = new IntersectionObserver((entries) => {
    let step = 0;
    for (const entry of entries) {
      if (!entry.isIntersecting || entry.target.hidden) continue;
      entry.target.classList.add("is-revealed", `reveal-step-${step++ % 3}`);
      observer.unobserve(entry.target);
    }
  }, { threshold: 0.08 });
  document.addEventListener("animationend", (event) => {
    if (["assemble-card", "enter-from-right", "enter-from-left"].includes(event.animationName)) finishReveal(event.target);
  });
  targets.forEach((target) => observer.observe(target));
  motion.addEventListener("change", (event) => {
    if (!event.matches) return;
    observer.disconnect();
    targets.forEach(finishReveal);
  });
})();
