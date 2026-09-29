const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const code = fs.readFileSync(require("node:path").join(__dirname, "../static/catalog.js"), "utf8");

function node(dataset = {}) {
  const attrs = { "aria-pressed": "true" }, events = {}, classes = new Set();
  return { dataset, hidden: false, disabled: true, textContent: "",
    classList: { add: (...items) => items.forEach((item) => classes.add(item)),
      remove: (...items) => items.forEach((item) => classes.delete(item)), contains: (item) => classes.has(item) },
    getAttribute: (key) => attrs[key], setAttribute: (key, value) => { attrs[key] = value; },
    addEventListener: (event, cb) => { events[event] = cb; },
    click() { events.click(); }, focus() { this.focused = true; } };
}
function setup(reduced = true, observerAvailable = true) {
  const choices = ["a", "b", "c"].map((value) => node({ filter: "aggregator", value }))
    .concat(["P", "Q", "R"].map((value) => node({ filter: "provider", value })));
  const cards = [
    node({ provider: "P", aggregators: "a b" }),
    node({ provider: "Q", aggregators: "a" }),
    node({ provider: "R", aggregators: "c" }),
    node({ provider: "anonymous", aggregators: "b" })
  ];
  const reset = node(), status = node(), empty = node();
  const media = { matches: reduced, addEventListener: (_event, cb) => { media.change = cb; } };
  let observer;
  class Observer {
    constructor(callback, options) { Object.assign(this, { callback, options, watched: new Set() }); observer = this; }
    observe(target) { this.watched.add(target); }
    unobserve(target) { this.watched.delete(target); }
    disconnect() { this.watched.clear(); this.disconnected = true; }
  }
  const window = { matchMedia: () => media };
  if (observerAvailable) window.IntersectionObserver = Observer;
  const documentEvents = {};
  const document = {
    addEventListener: (event, callback) => { documentEvents[event] = callback; },
    querySelectorAll: (selector) => selector === ".tile-choice" ? choices : selector === ".model-card" ? cards : [...choices, ...cards],
    getElementById: (id) => ({ "reset-choices": reset, "selection-status": status, "selection-empty": empty })[id]
  };
  vm.runInNewContext(code, { window, document, IntersectionObserver: Observer });
  const pick = (kind, value) => choices.find((item) => item.dataset.filter === kind && item.dataset.value === value);
  const selected = (kind) => choices.filter((item) => item.dataset.filter === kind && item.getAttribute("aria-pressed") === "true").map((item) => item.dataset.value);
  return { choices, cards, reset, empty, status, pick, selected, media, observer, documentEvents,
    visible: () => cards.map((card, index) => card.hidden ? null : index).filter((index) => index !== null) };
}

test("all selected initially, including anonymous models", () => {
  const ui = setup();
  assert.deepEqual(ui.visible(), [0, 1, 2, 3]);
  assert.equal(ui.reset.hidden, true);
  assert.equal(ui.status.textContent, "4 models shown");
  assert.ok(ui.choices.every((button) => !button.disabled));
});
test("first aggregator click isolates it and selects only its providers", () => {
  const ui = setup(); ui.pick("aggregator", "a").click();
  assert.deepEqual(ui.selected("aggregator"), ["a"]);
  assert.deepEqual(ui.selected("provider"), ["P", "Q"]);
  assert.deepEqual(ui.visible(), [0, 1]);
  assert.equal(ui.reset.hidden, false);
});
test("first provider click selects its matching aggregators", () => {
  const ui = setup(); ui.pick("provider", "P").click();
  assert.deepEqual(ui.selected("provider"), ["P"]);
  assert.deepEqual(ui.selected("aggregator"), ["a", "b"]);
  assert.deepEqual(ui.visible(), [0]);
});
test("subsequent clicks add, and the latest click expands the other group", () => {
  const ui = setup(); ui.pick("aggregator", "a").click(); ui.pick("provider", "R").click();
  assert.deepEqual(ui.selected("aggregator"), ["a", "c"]);
  assert.deepEqual(ui.selected("provider"), ["P", "Q", "R"]);
  assert.deepEqual(ui.visible(), [0, 1, 2]);
});
test("clicking selected tile removes it and prunes incompatible opposite tiles", () => {
  const ui = setup(); ui.pick("aggregator", "a").click(); ui.pick("provider", "P").click();
  assert.deepEqual(ui.selected("provider"), ["Q"]);
  assert.deepEqual(ui.selected("aggregator"), ["a"]);
  assert.deepEqual(ui.visible(), [1]);
  ui.pick("aggregator", "a").click();
  assert.deepEqual(ui.visible(), []);
  assert.equal(ui.empty.hidden, false);
});
test("adding all tiles restores initial state and hides Select all", () => {
  const ui = setup(); ui.pick("aggregator", "a").click();
  ui.pick("aggregator", "b").click(); ui.pick("aggregator", "c").click();
  assert.deepEqual(ui.visible(), [0, 1, 2, 3]);
  assert.equal(ui.reset.hidden, true);
  ui.pick("provider", "R").click();
  assert.deepEqual(ui.visible(), [2]);
});
test("Select all restores both groups and moves focus off the hidden control", () => {
  const ui = setup(); ui.pick("provider", "P").click(); ui.reset.click();
  assert.deepEqual(ui.visible(), [0, 1, 2, 3]);
  assert.equal(ui.reset.hidden, true);
  assert.equal(ui.choices[0].focused, true);
});
test("anonymous models are associated with gateways, not a fictional provider tile", () => {
  const ui = setup(); ui.pick("aggregator", "b").click();
  assert.deepEqual(ui.visible(), [0, 3]);
  assert.deepEqual(ui.selected("provider"), ["P"]);
});
test("scroll assembly is one-shot and disabled by reduced motion", () => {
  assert.equal(setup().observer, undefined);
  assert.equal(setup(false, false).observer, undefined);
  const ui = setup(false), target = ui.cards[0];
  assert.equal(ui.observer.options.threshold, 0.08);
  ui.observer.callback([{ target, isIntersecting: true }]);
  assert.ok(target.classList.contains("is-revealed"));
  assert.equal(ui.observer.watched.has(target), false);
  ui.media.change({ matches: true });
  assert.ok(ui.observer.disconnected);
  assert.equal(target.classList.contains("is-revealed"), false);
});

test("completed assembly removes animation styles so filtering cannot replay it", () => {
  const ui = setup(false), target = ui.cards[0];
  ui.observer.callback([{ target, isIntersecting: true }]);
  ui.documentEvents.animationend({ target, animationName: "assemble-copy" });
  assert.ok(target.classList.contains("is-revealed"));
  ui.documentEvents.animationend({ target, animationName: "assemble-card" });
  assert.equal(target.classList.contains("is-revealed"), false);
  assert.equal(target.classList.contains("reveal-step-0"), false);
  ui.pick("provider", "R").click(); ui.reset.click();
  assert.equal(target.hidden, false);
  assert.equal(target.classList.contains("is-revealed"), false);
  assert.equal(ui.observer.watched.has(target), false);
});
test("filtering during assembly cleans up; unseen cards can still reveal later", () => {
  const ui = setup(false), seen = ui.cards[0], unseen = ui.cards[1];
  ui.observer.callback([{ target: seen, isIntersecting: true }]);
  ui.pick("provider", "R").click();
  assert.ok(seen.hidden && unseen.hidden);
  assert.equal(seen.classList.contains("is-revealed"), false);
  ui.observer.callback([{ target: unseen, isIntersecting: true }]);
  assert.ok(ui.observer.watched.has(unseen));
  ui.reset.click();
  ui.observer.callback([{ target: unseen, isIntersecting: true }]);
  assert.ok(unseen.classList.contains("is-revealed"));
  assert.equal(seen.classList.contains("is-revealed"), false);
});
test("mixed click sequences preserve card order and compatible selections", () => {
  const ui = setup(), original = ui.cards.map((card) => ({ ...card.dataset }));
  let seed = 17;
  for (let step = 0; step < 300; step++) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    if (step % 29 === 0) ui.reset.click();
    else ui.choices[seed % ui.choices.length].click();
    const providers = ui.selected("provider"), aggregators = ui.selected("aggregator");
    assert.equal(ui.reset.hidden, providers.length === 3 && aggregators.length === 3);
    for (const provider of providers) {
      assert.ok(ui.cards.some((card) => card.dataset.provider === provider && !card.hidden));
    }
    for (const aggregator of aggregators) {
      assert.ok(ui.cards.some((card) => card.dataset.aggregators.split(" ").includes(aggregator) && !card.hidden));
    }
    assert.deepEqual(ui.cards.map((card) => card.dataset), original);
    assert.equal(ui.empty.hidden, ui.visible().length !== 0);
  }
});
