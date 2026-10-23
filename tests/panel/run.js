/*
 * Tests of the panel's pure functions (docs/PANEL.md 5): `node tests/panel/run.js`. Node is a dev dependency only;
 * the extension itself never runs under Node. Exit code 1 on any failure.
 */
"use strict";

var assert = require("assert");
var fs = require("fs");
var path = require("path");

var ROOT = path.resolve(__dirname, "..", "..");
var PANEL = path.join(ROOT, "src", "premiere_deck", "panel");
var JSX = path.join(ROOT, "src", "premiere_deck", "premiere", "jsx");

var common = require(path.join(PANEL, "js", "common.js"));
var agent = require(path.join(PANEL, "js", "agent.js"));
var display = require(path.join(PANEL, "js", "display.js"));

var tests = [];
function test(name, fn) {
    tests.push({name: name, fn: fn});
}

// --- a minimal DOM for applyOps and the display's event handling -------------------------------------------------

function FakeNode(doc, tag, ns) {
    this.ownerDocument = doc;
    this.tagName = tag;
    this.namespace = ns || null;
    this.nodeType = 1;
    this.attributes = {};
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this.offsetWidth = 188;
    this.offsetHeight = 48;
    this._text = "";
    this.style = {};
}
Object.defineProperty(FakeNode.prototype, "firstChild", {get: function () { return this.childNodes[0] || null; }});
Object.defineProperty(FakeNode.prototype, "nextSibling", {get: function () {
    if (!this.parentNode) {
        return null;
    }
    var list = this.parentNode.childNodes;
    return list[list.indexOf(this) + 1] || null;
}});
Object.defineProperty(FakeNode.prototype, "className", {
    get: function () { return this.attributes["class"] || ""; },
    set: function (value) { this.attributes["class"] = String(value); }
});
Object.defineProperty(FakeNode.prototype, "textContent", {
    get: function () {
        return this.childNodes.length ? this.childNodes.map(function (c) { return c.textContent; }).join("") : this._text;
    },
    set: function (value) {
        this.childNodes.forEach(function (c) { c.parentNode = null; });
        this.childNodes = [];
        this._text = String(value);
    }
});
FakeNode.prototype.setAttribute = function (name, value) { this.attributes[name] = String(value); };
FakeNode.prototype.removeAttribute = function (name) { delete this.attributes[name]; };
FakeNode.prototype.getAttribute = function (name) { return this.attributes.hasOwnProperty(name) ? this.attributes[name] : null; };
FakeNode.prototype.hasAttribute = function (name) { return this.attributes.hasOwnProperty(name); };
FakeNode.prototype.detach = function (child) {
    if (child.parentNode) {
        var list = child.parentNode.childNodes;
        list.splice(list.indexOf(child), 1);
        child.parentNode = null;
    }
};
FakeNode.prototype.appendChild = function (child) { return this.insertBefore(child, null); };
FakeNode.prototype.insertBefore = function (child, ref) {
    this.detach(child);
    var at = ref ? this.childNodes.indexOf(ref) : -1;
    if (at < 0) {
        this.childNodes.push(child);
    } else {
        this.childNodes.splice(at, 0, child);
    }
    child.parentNode = this;
    return child;
};
FakeNode.prototype.removeChild = function (child) { this.detach(child); return child; };
FakeNode.prototype.replaceChild = function (child, old) {
    this.insertBefore(child, old);
    this.detach(old);
    return old;
};
FakeNode.prototype.contains = function (other) {
    for (var n = other; n; n = n.parentNode) {
        if (n === this) {
            return true;
        }
    }
    return false;
};
FakeNode.prototype.addEventListener = function (type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); };
FakeNode.prototype.getBoundingClientRect = function () { return {left: 10, top: 300, width: 100, height: 60}; };
// The two selector forms the display uses: [attr="value"] and .class.
FakeNode.prototype.querySelector = function (selector) {
    var self = this;
    var attr = /^\[([\w-]+)="(.*)"\]$/.exec(selector);
    var klass = /^\.([\w-]+)$/.exec(selector);
    return find(this, function (n) {
        if (n === self) {
            return false;
        }
        if (attr) {
            return n.attributes[attr[1]] === attr[2].replace(/\\(["\\])/g, "$1");
        }
        return klass ? (" " + (n.attributes["class"] || "") + " ").indexOf(" " + klass[1] + " ") >= 0 : false;
    });
};
// A class selector only (`.name`): every match in document order.
FakeNode.prototype.querySelectorAll = function (selector) {
    var klass = /^\.([\w-]+)$/.exec(selector);
    var self = this;
    return klass ? findAll(this, function (n) { return n !== self && (" " + (n.attributes["class"] || "") + " ").indexOf(" " + klass[1] + " ") >= 0; }) : [];
};
function fakeDocument() {
    var doc = {
        createElement: function (tag) { return new FakeNode(doc, tag); },
        createElementNS: function (ns, tag) { return new FakeNode(doc, tag, ns); }
    };
    return doc;
}
function find(node, predicate) {
    if (predicate(node)) {
        return node;
    }
    for (var i = 0; i < node.childNodes.length; i++) {
        var found = find(node.childNodes[i], predicate);
        if (found) {
            return found;
        }
    }
    return null;
}
function findAll(node, predicate, out) {
    out = out || [];
    if (predicate(node)) {
        out.push(node);
    }
    node.childNodes.forEach(function (c) { findAll(c, predicate, out); });
    return out;
}
function findV(vnode, predicate, out) {
    out = out || [];
    if (predicate(vnode)) {
        out.push(vnode);
    }
    vnode.c.forEach(function (c) { findV(c, predicate, out); });
    return out;
}
function byAction(vnode, id) {
    return findV(vnode, function (v) { return v.a["data-action"] === id; });
}
function hasClass(vnode, name) {
    return (" " + (vnode.a["class"] || "") + " ").indexOf(" " + name + " ") >= 0;
}
function byClass(vnode, name) {
    return findV(vnode, function (v) { return hasClass(v, name); });
}
function textOf(vnode) {
    return vnode.x !== null ? vnode.x : vnode.c.map(textOf).join("");
}
function serialise(node) {
    return {t: node.tagName, ns: node.namespace, a: node.attributes, x: node.childNodes.length ? null : node._text,
        c: node.childNodes.map(serialise)};
}

// A stand-in window for Display: storage, a document with <html>, reduced motion and the View Transitions switch.
function fakeWindow(options) {
    options = options || {};
    var store = Object.assign({}, options.storage || {});
    var doc = fakeDocument();
    var classes = {};
    doc.documentElement = {
        attrs: {"data-theme": options.theme || "black"},
        setAttribute: function (name, value) { this.attrs[name] = value; },
        getAttribute: function (name) { return this.attrs.hasOwnProperty(name) ? this.attrs[name] : null; },
        classList: {add: function (c) { classes[c] = true; }, remove: function (c) { delete classes[c]; },
            contains: function (c) { return !!classes[c]; }}
    };
    doc.body = new FakeNode(doc, "body");
    doc.transitions = 0;
    if (options.viewTransitions) {
        doc.startViewTransition = function (update) {
            doc.transitions++;
            update();
            return {finished: {then: function (ok) { ok(); }}};
        };
    }
    return {
        document: doc,
        classes: classes,
        store: store,
        DeckCommon: common,
        innerWidth: options.innerWidth || 360,
        innerHeight: options.innerHeight || 640,
        devicePixelRatio: options.pixelRatio || 1,
        addEventListener: function () {},
        matchMedia: function () { return {matches: !!options.reducedMotion}; },
        localStorage: {
            getItem: function (key) { return store.hasOwnProperty(key) ? store[key] : null; },
            setItem: function (key, value) { store[key] = String(value); },
            key: function (i) { return Object.keys(store)[i] || null; },
            get length() { return Object.keys(store).length; }
        }
    };
}

// Fake timers for the display's setTimeout (restored by `run`).
function withTimers(run) {
    var timers = [];
    var realSet = global.setTimeout;
    var realClear = global.clearTimeout;
    var now = 0;
    global.setTimeout = function (fn, ms) {
        timers.push({fn: fn, at: now + (ms || 0), id: timers.length + 1, live: true});
        return timers.length;
    };
    global.clearTimeout = function (id) {
        if (timers[id - 1]) {
            timers[id - 1].live = false;
        }
    };
    var clock = {
        timers: timers,
        advance: function (ms) {
            now += ms;
            var due = timers.filter(function (t) { return t.live && t.at <= now; });
            due.forEach(function (t) { t.live = false; t.fn(); });
        },
        pending: function () { return timers.filter(function (t) { return t.live; }).length; }
    };
    try {
        run(clock);
    } finally {
        global.setTimeout = realSet;
        global.clearTimeout = realClear;
    }
}

// A display on a fake container: `sent` collects the messages it would send to the host.
function makeDisplay(options) {
    var win = fakeWindow(options);
    var doc = win.document;
    var container = new FakeNode(doc, "div");
    container.getBoundingClientRect = function () { return {left: 0, top: 0, width: 360, height: 640}; };
    var d = new display.Display(container, win);
    var sent = [];
    d.client = {send: function (message) { sent.push(message); return true; }, retryNow: function () {}};
    d.bindEvents();
    return {d: d, win: win, container: container, sent: sent};
}
function fire(container, type, event) {
    (container.listeners[type] || []).forEach(function (fn) { fn(event); });
}
function clickOn(ctx, predicate) {
    var el = find(ctx.container, predicate);
    assert.ok(el, "an element to click");
    fire(ctx.container, "click", {target: el});
    return el;
}
function byAttr(name, value) {
    return function (n) { return n.attributes[name] === value; };
}
// 3.0 tiles: a wrapper (`data-tile`, `data-zone`) around the body button (`data-action`) and its extra targets.
function byTile(vnode, id, zone) {
    return findV(vnode, function (v) { return v.a["data-tile"] === id && (!zone || v.a["data-zone"] === zone); });
}
function tileOf(vnode, id) {
    return byTile(vnode, id).filter(function (v) { return v.a["data-zone"] !== "pins"; })[0];
}
function bodyOf(vnode, id) {
    return byClass(tileOf(vnode, id), "deck-t-body")[0];
}
function domTile(ctx, id, zone) {
    return find(ctx.container, byAttr("data-anchor", (zone || "lane") + ":" + id));
}
// The body button of a tile in the DOM: a press on it runs the tile.
function domBody(ctx, id, zone) {
    var wrap = domTile(ctx, id, zone);
    return wrap ? find(wrap, byAttr("data-action", id)) : null;
}
function press(ctx, el) {
    assert.ok(el, "an element to press");
    fire(ctx.container, "click", {target: el});
}

// --- fixtures -------------------------------------------------------------------------------------------------

function filesSection() {
    return {id: "files", title: "Files", icon: "folder", collapsed: false, status: {text: "Watching · A", tone: "ok"}, items: [
        {kind: "row", items: [
            {kind: "text", text: "spring-promo_v2", tone: "neutral", small: false},
            {kind: "button", id: "files.open_folder", label: "Open project folder", hint: "Opens the project folder in Explorer.",
                icon: "open-folder", variant: "icon"}
        ]},
        {kind: "text", text: "D:\\Footage\\spring-promo", tone: "neutral", small: true},
        {kind: "row", items: [
            {kind: "text", text: "142 in the root", tone: "ok", small: true, chip: true},
            {kind: "text", text: "4 outside", tone: "warn", small: true, chip: true},
            {kind: "text", text: "1 offline", tone: "error", small: true, chip: true}
        ]},
        {kind: "group", id: "files.actions", title: null, columns: 2, tiles: true, items: [
            {kind: "button", id: "files.sort", label: "Sort project", short: "Sort project", icon: "sort", meta: "18 items to file",
                hint: "Moves loose media into the layout folders.", confirm: "Sort 18 items?", tone: "neutral"},
            {kind: "button", id: "files.sync_all", label: "Sync all media", short: "Sync all media", icon: "sync-all", meta: "4 outside the root"},
            {kind: "button", id: "files.sync_used", label: "Sync used media", short: "Sync used media", icon: "sync-used", meta: "2 in sequences"},
            {kind: "button", id: "files.zip", label: "Make ZIP", short: "Make ZIP", icon: "zip", meta: "Checks offline first"}
        ]},
        {kind: "progress", value: 0.27, label: "Sorting project 38 / 142", detail: "C0042.MP4 > Footage\\Camera A", cancel: "files.stop"},
        {kind: "row", items: [
            {kind: "button", id: "files.journal", label: "Journal · 24 entries", icon: "journal"},
            {kind: "button", id: "files.undo_last", label: "Undo last", icon: "undo", variant: "icon", hint: "Undoes the last change."}
        ]},
        {kind: "group", id: "files.offline", title: "Offline", collapsible: true, items: [
            {kind: "list", id: "files.offline_list", empty: "", rows: [
                {id: "o1", title: "C0042.MP4", subtitle: "Footage\\Camera A", tone: "warn", actions: [{kind: "button", id: "files.locate", label: "Locate"}]},
                {id: "o2", title: "C0043.MP4", subtitle: "Footage\\Camera A", tone: "warn", actions: [{kind: "button", id: "files.locate", label: "Locate"}]}
            ]}
        ]}
    ]};
}
function actionsSection() {
    return {id: "actions", title: "Actions", icon: "actions", collapsed: false, status: null, items: [
        {kind: "group", id: "actions.timeline", title: "Timeline", columns: 3, tiles: true, collapsible: true, items: [
            {kind: "button", id: "actions.adjustment_layer", label: "Adjustment layer", short: "Adjustment layer", icon: "adjustment-layer",
                hint: "Adds an adjustment layer over the selected clips."},
            {kind: "button", id: "actions.razor_all", label: "Razor all tracks", short: "Razor all", icon: "razor-all", hint: "Cuts every unlocked track at the playhead."},
            {kind: "button", id: "actions.delete_empty_tracks", label: "Delete empty tracks", short: "Empty tracks", icon: "delete-empty-tracks"},
            {kind: "button", id: "actions.solo_track", label: "Solo audio track", short: "Solo track", icon: "solo-track", tone: "accent"}
        ]},
        {kind: "group", id: "actions.sequence", title: "Sequence", columns: 3, tiles: true, collapsible: true, items: [
            {kind: "button", id: "actions.duplicate_sequence", label: "Duplicate sequence", short: "Duplicate", icon: "duplicate-sequence"},
            {kind: "button", id: "actions.social_copy", label: "Social copy", short: "Social copy", icon: "social-copy", hint: "Creates a reframed copy.",
                menu: {id: "actions.social_copy.format", value: "9x16", badge: "9:16",
                    options: [{value: "9x16", label: "9:16"}, {value: "1x1", label: "1:1"}, {value: "4x5", label: "4:5"}]}}
        ]},
        {kind: "group", id: "actions.export", title: "Export", columns: 3, tiles: true, collapsible: true, items: [
            {kind: "button", id: "actions.export_frame", label: "Export frame", short: "Export frame", icon: "export-frame"}
        ]},
        {kind: "group", id: "actions.tracks", title: "Tracks", columns: 3, tiles: true, collapsible: true, items: [
            {kind: "button", id: "actions.add_video_track", label: "Add video track", short: "+V", icon: "track-video-add"},
            {kind: "switch", id: "actions.solo_audio", label: "Solo audio", short: "SOLO", icon: "solo-track", value: false,
                hint: "Mutes every other audio track."},
            {kind: "switch", id: "actions.lock_others", label: "Lock other tracks", short: "LOCK", icon: "lock-others", value: true}
        ]}
    ]};
}
function miscSection() {
    return {id: "misc", title: "Misc", collapsed: false, status: null, items: [
        {kind: "button", id: "misc.collect", label: "Collect", tone: "accent", busy: false, confirm: "Copy 14 files?", hint: "Copies the files."},
        {kind: "button", id: "misc.busy", label: "Busy", busy: true},
        {kind: "toggle", id: "misc.watch", label: "Watch", value: true, hint: "Watches the folder."},
        {kind: "select", id: "misc.layout", label: "Layout", value: "b", options: [{value: "a", label: "A"}, {value: "b", label: "B"}]},
        {kind: "input", id: "misc.find", label: "Find", value: "car", placeholder: "Search", submit_label: "Go"},
        {kind: "text", text: "Hello", tone: "warn", small: true},
        {kind: "progress", value: null, label: "Indeterminate"},
        {kind: "row", items: [{kind: "button", id: "misc.a", label: "A"}, {kind: "button", id: "misc.b", label: "B", disabled: true}]},
        {kind: "divider"},
        {kind: "group", id: "misc.two", title: "Two columns", columns: 2, items: [
            {kind: "button", id: "misc.g1", label: "One"}, {kind: "button", id: "misc.g2", label: "Two"},
            {kind: "button", id: "misc.g3", label: "Three", confirm: "Run three?"},
            {kind: "text", text: "A text breaks the run", small: true},
            {kind: "button", id: "misc.g4", label: "Four"}
        ]},
        {kind: "list", id: "misc.results", empty: "Nothing", rows: [
            {id: "r1", title: "One", subtitle: "sub", tone: "ok", thumb: null, bar: 0.5, actions: [{kind: "button", id: "misc.open", label: "Open"}]},
            {id: "r2", title: "Two", subtitle: "", tone: "neutral", thumb: "data:image/png;base64,AA==",
                actions: [{kind: "button", id: "misc.open", label: "Open", confirm: "Sure?"}]}
        ]}
    ]};
}
function sampleTree(overrides) {
    var tree = {
        type: "ui", revision: 3, title: "Premiere Deck", status: {text: "Connected · A.prproj", tone: "ok"},
        sections: [filesSection(), actionsSection(), miscSection()],
        footer: {text: "Premiere Deck 2.0.0", link: {label: "Open host", action: "host.open"}}
    };
    return Object.assign(tree, overrides || {});
}
function local(extra) {
    return Object.assign({connection: "connected", tab: "files", collapsed: {}, flash: {}, lines: {}, detailOpen: {},
        confirming: null, menu: null, tip: null}, extra || {});
}
var CONNECTED = local();
function rendered(tab, extra) {
    return display.renderTree(sampleTree(), local(Object.assign({tab: tab}, extra || {})));
}

// --- common.js ------------------------------------------------------------------------------------------------

test("retryDelay: 3 s for a minute, then 15 s", function () {
    assert.strictEqual(common.retryDelay(0), 3000);
    assert.strictEqual(common.retryDelay(59999), 3000);
    assert.strictEqual(common.retryDelay(60000), 15000);
});

test("parseDeckJson accepts the discovery file and rejects broken ones", function () {
    var deck = common.parseDeckJson('{"port": 47800, "token": "ab", "host_version": "1.0.0", "protocol": 1, "host_exe": "C:\\\\x.exe", "autostart_host": true}');
    assert.strictEqual(deck.port, 47800);
    assert.strictEqual(deck.host_exe, "C:\\x.exe");
    assert.strictEqual(deck.autostart_host, true);
    assert.strictEqual(common.parseDeckJson("not json"), null);
    assert.strictEqual(common.parseDeckJson('{"port": 0, "token": "ab"}'), null);
    assert.strictEqual(common.parseDeckJson('{"port": 47800}'), null);
    assert.strictEqual(common.parseDeckJson('{"port": 47800, "token": "t", "host_exe": ""}').host_exe, null);
});

test("deckChanged: another port or token is a new host run; a missing file is not", function () {
    var used = {port: 47800, token: "old", written_at: 1};
    assert.strictEqual(common.deckChanged(used, {port: 47800, token: "old", written_at: 2}), false);
    assert.strictEqual(common.deckChanged(used, {port: 47800, token: "new"}), true);
    assert.strictEqual(common.deckChanged(used, {port: 47801, token: "old"}), true);
    assert.strictEqual(common.deckChanged(used, null), false);
    assert.strictEqual(common.deckChanged(null, used), true);
    assert.ok(common.DECK_POLL_MS > 0 && common.DECK_POLL_MS <= 1000);
});

test("the agent never opens the visible panel", function () {
    var source = fs.readFileSync(path.join(PANEL, "js", "agent.js"), "utf8");
    assert.strictEqual(source.indexOf("requestOpenExtension"), -1);
});

test("socketUrl and helloMessage follow PROTOCOL 1 and 3", function () {
    assert.strictEqual(common.socketUrl({port: 47801, token: "a b"}, "agent"), "ws://127.0.0.1:47801/ws?role=agent&token=a%20b");
    var hello = common.helloMessage({role: "display", panel_version: "0.1.0", premiere_version: "26.5.2"});
    assert.deepStrictEqual(hello, {type: "hello", role: "display", panel_version: "0.1.0", protocol: 1,
        premiere_version: "26.5.2", cep_version: "", extension_id: "", project_path: null});
});

test("joinPath and shouldReload", function () {
    assert.strictEqual(common.joinPath("C:/ext", "deck.json"), "C:/ext/deck.json");
    assert.strictEqual(common.joinPath("C:\\ext\\", "VERSION"), "C:\\ext\\VERSION");
    assert.strictEqual(common.shouldReload(0, 1000), true);
    assert.strictEqual(common.shouldReload(1000, 20000), false);
    assert.strictEqual(common.shouldReload(1000, 40000), true);
});

// --- agent.js -------------------------------------------------------------------------------------------------

// Runs a wrapped program the way Premiere would, with host.jsx's helpers reimplemented on Node's JSON.
function runWrapped(code, args) {
    var __deckError = function (e, source, offset) {
        var line = e && e.line !== undefined ? e.line - (offset || 0) : null;
        return {__deck_error: true, message: String(e && e.message !== undefined ? e.message : e), line: line, source: source};
    };
    var __deckEncode = function (value) {
        if (value && value.__deck_error) {
            return JSON.stringify({ok: false, error: {message: value.message, line: value.line, source: value.source}});
        }
        return JSON.stringify({ok: true, result: value === undefined ? null : value});
    };
    var program = agent.wrapScript(code, args);
    // eslint-disable-next-line no-eval
    return eval(program);
}

test("wrapScript runs the code with __args and encodes the result", function () {
    var raw = runWrapped("return {sum: __args.a + __args.b};", {a: 1, b: 2});
    assert.deepStrictEqual(agent.parseResult(raw, "x"), {ok: true, result: {sum: 3}});
});

test("wrapScript allows function declarations (ExtendScript rejects them inside try)", function () {
    var program = agent.wrapScript("function twice(x) { return 2 * x; }\nreturn twice(__args);", 4);
    var body = program.split("\n");
    assert.strictEqual(body[2], "function __deckBody() {");
    assert.ok(program.indexOf("try { __r = __deckBody(); }") > 0);
    assert.strictEqual(agent.parseResult(runWrapped("function twice(x) { return 2 * x; }\nreturn twice(__args);", 4)).result, 8);
});

test("wrapScript maps errors with a line relative to the script", function () {
    var raw = runWrapped("var a = 1;\nthrow {message: 'boom', line: " + (agent.WRAPPER_LINES + 2) + "};", null);
    var outcome = agent.parseResult(raw, "var a = 1;");
    assert.strictEqual(outcome.ok, false);
    assert.strictEqual(outcome.error.message, "boom");
    assert.strictEqual(outcome.error.line, 2);
});

test("the script starts on line WRAPPER_LINES + 1", function () {
    var lines = agent.wrapScript("FIRST_LINE", null).split("\n");
    assert.strictEqual(lines[agent.WRAPPER_LINES], "FIRST_LINE");
});

test("jsLiteral escapes U+2028 and U+2029", function () {
    var text = agent.jsLiteral("a" + String.fromCharCode(0x2028) + "b" + String.fromCharCode(0x2029));
    assert.strictEqual(text.indexOf(String.fromCharCode(0x2028)), -1);
    assert.strictEqual(JSON.parse(text), "a" + String.fromCharCode(0x2028) + "b" + String.fromCharCode(0x2029));
    assert.strictEqual(agent.jsLiteral(undefined), "null");
});

test("parseResult covers every shape evalScript returns", function () {
    assert.strictEqual(agent.parseResult("", "c").ok, false);
    assert.strictEqual(agent.parseResult(undefined, "c").error.message, "empty result from evalScript");
    var syntax = agent.parseResult("EvalScript error.", "code");
    assert.strictEqual(syntax.syntax, true);
    assert.strictEqual(syntax.error.source, "code");
    assert.ok(/^unexpected result/.test(agent.parseResult("hello", "c").error.message));
    assert.ok(/^unexpected result/.test(agent.parseResult("[1]", "c").error.message));
    assert.deepStrictEqual(agent.parseResult('{"ok":true,"result":null}', "c"), {ok: true, result: null});
    var failed = agent.parseResult('{"ok":false,"error":{"message":"x","line":4,"source":"s"}}', "c");
    assert.deepStrictEqual(failed.error, {message: "x", line: 4, source: "s"});
});

test("utf8Length and splitUtf8 never split a character", function () {
    assert.strictEqual(agent.utf8Length("aé€😀"), 1 + 2 + 3 + 4);
    var text = "";
    for (var i = 0; i < 500; i++) {
        text += "aé€😀";
    }
    var chunks = agent.splitUtf8(text, 13);
    assert.strictEqual(chunks.join(""), text);
    chunks.forEach(function (chunk) {
        assert.ok(agent.utf8Length(chunk) <= 13);
        var last = chunk.charCodeAt(chunk.length - 1);
        assert.ok(!(last >= 0xd800 && last <= 0xdbff), "chunk ends inside a surrogate pair");
    });
    assert.deepStrictEqual(agent.splitUtf8("", 10), [""]);
});

test("resultMessages pages large results (PROTOCOL 6.4)", function () {
    var small = agent.resultMessages("q1", {ok: true, result: {a: 1}}, 5, 1000);
    assert.deepStrictEqual(small, [{type: "script.result", id: "q1", ok: true, result: {a: 1}, ms: 5}]);
    var big = {items: []};
    for (var i = 0; i < 200; i++) {
        big.items.push({name: "clipé " + i, path: "C:\\media\\" + i});
    }
    var pages = agent.resultMessages("q2", {ok: true, result: big}, 9, 1000);
    assert.ok(pages.length > 1);
    pages.forEach(function (page, index) {
        assert.strictEqual(page.page, index + 1);
        assert.strictEqual(page.pages, pages.length);
        assert.ok(agent.utf8Length(page.chunk) <= 1000);
    });
    assert.deepStrictEqual(JSON.parse(pages.map(function (p) { return p.chunk; }).join("")), big);
    var error = agent.resultMessages("q3", {ok: false, error: {message: "timeout", line: null, source: ""}}, 1);
    assert.deepStrictEqual(error, [{type: "script.result", id: "q3", ok: false, error: {message: "timeout", line: null, source: ""}, ms: 1}]);
});

test("event bindings map both ways", function () {
    var names = agent.bindingsFor(["sequence.structure_changed", "project.items_added", "sequence.structure_changed", "panel.opened"]);
    assert.ok(names.indexOf("onActiveSequenceChanged") >= 0);
    assert.ok(names.indexOf("onItemsAddedToProjectSuccess") >= 0);
    assert.strictEqual(names.length, new Set(names).size);
    assert.strictEqual(agent.eventForBinding("onActiveSequenceTrackItemAdded"), "sequence.structure_changed");
    assert.strictEqual(agent.eventForBinding("onSequenceActivated"), "sequence.activated");
    assert.strictEqual(agent.eventForBinding("onSomethingElse"), null);
});

test("pollEvents derives project events from two polls", function () {
    assert.deepStrictEqual(agent.pollEvents(null, {path: null, count: null}), []);
    assert.deepStrictEqual(agent.pollEvents(null, {path: "A", count: 3}), [{name: "project.opened", data: {path: "A"}}]);
    assert.deepStrictEqual(agent.pollEvents({path: "A", count: 3}, {path: "A", count: 4}), [{name: "project.items_added", data: {}}]);
    assert.deepStrictEqual(agent.pollEvents({path: "A", count: 4}, {path: "A", count: 2}), []);
    assert.deepStrictEqual(agent.pollEvents({path: "A", count: null}, {path: "A", count: 5}), []);
    assert.deepStrictEqual(agent.pollEvents({path: "A", count: 3}, {path: "B", count: 1}),
        [{name: "project.closed", data: {path: "A"}}, {name: "project.opened", data: {path: "B"}}]);
    assert.deepStrictEqual(agent.pollEvents({path: "B", count: 1}, {path: null, count: null}), [{name: "project.closed", data: {path: "B"}}]);
});

// --- display.js: renderTree -----------------------------------------------------------------------------------

function allAttributes(vnode, out) {
    out = out || [];
    Object.keys(vnode.a).forEach(function (name) { out.push(name); });
    vnode.c.forEach(function (c) { allAttributes(c, out); });
    return out;
}

test("renderTree renders every node kind", function () {
    var v = rendered("misc");
    ["misc.collect", "misc.busy", "misc.watch", "misc.layout", "misc.find", "misc.a", "misc.b", "misc.g1", "host.open"].forEach(function (id) {
        assert.strictEqual(byAction(v, id).length, 1, id);
    });
    assert.strictEqual(byAction(v, "misc.open").length, 2);
    assert.strictEqual(findV(v, function (n) { return n.t === "hr"; }).length, 1);
    assert.strictEqual(byClass(v, "deck-progress").length, 1);
    assert.ok(hasClass(byClass(v, "deck-progress")[0], "is-indeterminate"));
    assert.strictEqual(findV(v, function (n) { return n.t === "img"; }).length, 1);
    assert.strictEqual(findV(v, function (n) { return n.a["class"] === "deck-list-bar-fill" && n.a.style === "width: 50.0%"; }).length, 1);
    var text = findV(v, function (n) { return n.x === "Hello"; })[0];
    assert.ok(hasClass(text, "tone-warn") && hasClass(text, "is-small"));
    assert.strictEqual(byAction(v, "misc.watch")[0].p.checked, true);
    assert.strictEqual(byAction(v, "misc.layout")[0].p.value, "b");
    assert.strictEqual(byAction(v, "misc.find")[0].p.value, "car");
    assert.strictEqual(findV(v, function (n) { return n.a["data-submit"] === "misc.find"; }).length, 1);
});

test("the frame: glyph, tabs, connection dot, footer with the version, the scale slider and Open host", function () {
    var v = rendered("files");
    var header = findV(v, function (n) { return n.t === "header"; })[0];
    assert.strictEqual(findV(header, function (n) { return n.t === "svg" && hasClass(n, "deck-glyph"); }).length, 1);
    assert.strictEqual(findV(header, function (n) { return n.t === "use" && n.a.href === "#glyph-pr"; }).length, 1);
    var dot = byClass(header, "deck-conn")[0];
    assert.ok(hasClass(dot, "tone-ok"));
    assert.strictEqual(dot.a["data-tip-hint"], "Connected · A.prproj", "the host status is the dot's tooltip");
    var footer = findV(v, function (n) { return n.t === "footer"; })[0];
    assert.deepStrictEqual(footer.c.map(function (c) { return c.a["class"].split(" ")[0]; }), ["deck-ver", "deck-scale", "deck-small-btn"],
        "version · scale · Open host");
    assert.strictEqual(textOf(byClass(footer, "deck-ver")[0]), "v2.0.0", "the version of the footer text, in mono");
    assert.strictEqual(display.footerVersion("Premiere Deck 3.0.0 (dev)"), "v3.0.0");
    assert.strictEqual(display.footerVersion("No version here"), "No version here");
    var link = byAction(footer, "host.open")[0];
    assert.strictEqual(textOf(link), "Open host");
    assert.strictEqual(link.p.disabled, false);
    // The whole panel is one stage of 360 px; its zoom is the scale.
    var stage = byClass(v, "deck-stage")[0];
    assert.strictEqual(stage.a.style, "zoom: 1; min-height: 640px");
    assert.ok(stage.c.indexOf(header) >= 0 && stage.c.indexOf(footer) >= 0, "the bars live on the stage");
});

test("tabs: one per section in order, icon and title, a sliding thumb, remembered and falling back", function () {
    var v = rendered("actions");
    var tabs = byClass(v, "deck-tab");
    assert.deepStrictEqual(tabs.map(function (t) { return t.a["data-tab"]; }), ["files", "actions", "misc"]);
    assert.deepStrictEqual(tabs.map(textOf), ["Files", "Actions", "Misc"]);
    assert.deepStrictEqual(tabs.map(function (t) { return hasClass(t, "is-on"); }), [false, true, false]);
    assert.deepStrictEqual(tabs.map(function (t) { return t.a["aria-selected"]; }), ["false", "true", "false"]);
    assert.deepStrictEqual(findV(tabs[0], function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }), ["#i-folder"]);
    assert.deepStrictEqual(findV(tabs[1], function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }), ["#i-actions"]);
    assert.strictEqual(findV(tabs[2], function (n) { return n.t === "use"; }).length, 0, "a section without an icon has none");
    var track = byClass(v, "deck-tabs")[0];
    assert.strictEqual(track.a.style, "--n: 3; --i: 1");
    assert.strictEqual(byClass(track, "deck-tabs-thumb").length, 1);
    assert.strictEqual(track.a["data-on"], "actions");
    // The body is that section only.
    assert.strictEqual(byAction(v, "actions.razor_all").length, 1);
    assert.strictEqual(byAction(v, "files.sort").length, 0);
    assert.strictEqual(byClass(v, "deck-tabpanel").length, 1);
    // A tab that is not in the tree falls back on the first section; none stored starts on the first.
    assert.strictEqual(byClass(rendered("gone"), "deck-tabs")[0].a["data-on"], "files");
    assert.strictEqual(byClass(rendered(null), "deck-tabs")[0].a["data-on"], "files");
    assert.strictEqual(display.selectedTab(sampleTree().sections, {tab: "misc"}), 2);
    assert.strictEqual(display.selectedTab([], {}), -1);
    var none = display.renderTree({type: "ui", revision: 1, title: "Premiere Deck", status: null, sections: []}, local());
    assert.strictEqual(byClass(none, "deck-tabs").length, 0);
});

test("a section status is accepted and not drawn", function () {
    var v = rendered("files");
    assert.strictEqual(byClass(v, "deck-status").length, 0);
    assert.strictEqual(findV(v, function (n) { return n.x === "Watching · A"; }).length, 0);
    var panel = byClass(v, "deck-tabpanel")[0];
    assert.ok(hasClass(panel.c[0], "is-head"), "the tab starts with its first node");
});

test("lanes: one row per titled tile group, its code at the left edge, fixed tiles with the icon over the code", function () {
    var v = rendered("actions");
    var lanes = byClass(v, "deck-lane-wrap").filter(function (l) { return !hasClass(l, "is-pins"); });
    assert.deepStrictEqual(lanes.map(function (l) { return l.a["data-lane"]; }),
        ["actions.timeline", "actions.sequence", "actions.export", "actions.tracks"]);
    // A group without `short` takes the first four letters of its title; the title is the code's tooltip.
    assert.deepStrictEqual(lanes.map(function (l) { return textOf(byClass(l, "deck-lane-code")[0]); }), ["TIME", "SEQU", "EXPO", "TRAC"]);
    assert.strictEqual(byClass(lanes[0], "deck-lane-code")[0].a["data-tip-label"], "Timeline");
    var tree = sampleTree();
    tree.sections[1].items[3].short = "TRKS";
    assert.strictEqual(textOf(byClass(byClass(display.renderTree(tree, local({tab: "actions"})), "deck-lane-wrap")[4], "deck-lane-code")[0]), "TRKS");
    assert.strictEqual(display.laneCode({title: "Keys"}), "KEYS");
    assert.strictEqual(display.laneCode({title: "Sel ection", short: "SEL"}), "SEL");
    // The lane's tiles sit in one fixed grid in the group's order.
    var grid = byClass(lanes[0], "deck-lane-tiles")[0];
    assert.deepStrictEqual(grid.c.map(function (c) { return c.a["data-tile"]; }),
        ["actions.adjustment_layer", "actions.razor_all", "actions.delete_empty_tracks", "actions.solo_track"]);
    var tile = tileOf(v, "actions.razor_all");
    assert.strictEqual(tile.t, "div");
    assert.ok(hasClass(tile, "deck-t") && hasClass(tile, "is-lane"));
    var body = byClass(tile, "deck-t-body")[0];
    assert.strictEqual(body.t, "button");
    assert.strictEqual(body.a["data-action"], "actions.razor_all");
    assert.strictEqual(body.a["aria-label"], "Razor all tracks");
    assert.deepStrictEqual(body.c.map(function (c) { return c.a["class"]; }), ["deck-face", "deck-t-code"], "icon over the code");
    assert.strictEqual(textOf(byClass(body, "deck-t-code")[0]), "Razor all");
    assert.deepStrictEqual(findV(byClass(body, "deck-face")[0], function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }),
        ["#i-razor-all", "#i-check", "#i-cross", "#i-warn"]);
    assert.ok(hasClass(tileOf(v, "actions.solo_track"), "tone-accent"));
    // Collapsing is a caption's job: a lane has none and always shows its tiles.
    assert.strictEqual(byClass(v, "deck-g-head").length, 0);
    assert.ok(tileOf(rendered("actions", {collapsed: {"actions.timeline": true}}), "actions.razor_all"));
    // Fixed tracks: 46 x 44 tiles, six per lane, never stretched.
    var css = fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
    assert.ok(/--tile-w: 46px;/.test(css) && /--tile-h: 44px;/.test(css) && /--lane-cols: 6;/.test(css));
    assert.ok(/\.deck-lane-tiles \{[^}]*grid-template-columns: repeat\(var\(--lane-cols\), var\(--tile-w\)\)/.test(css));
    assert.ok(/\.deck-rowtiles \{[^}]*grid-template-columns: repeat\(2, var\(--row-w\)\)/.test(css));
    assert.ok(/--row-w: 165px;/.test(css) && /--row-h: 46px;/.test(css));
});

test("Files: row tiles two per row, icon beside the code; the meta never shows; a tile without an icon draws no icon", function () {
    var v = rendered("files");
    var grid = byClass(v, "deck-rowtiles")[0];
    assert.deepStrictEqual(grid.c.map(function (c) { return c.a["data-tile"]; }), ["files.sort", "files.sync_all", "files.sync_used", "files.zip"]);
    grid.c.forEach(function (c) { assert.ok(hasClass(c, "is-row"), c.a["data-tile"]); });
    var body = bodyOf(v, "files.sync_all");
    assert.strictEqual(body.a["data-tip-label"], "Sync all media");
    assert.strictEqual(body.a["data-tip-hint"], "");
    assert.strictEqual(textOf(byClass(body, "deck-t-code")[0]), "Sync all media");
    assert.strictEqual(allAttributes(v).indexOf("data-tip-meta"), -1, "no meta anywhere");
    assert.strictEqual(findV(v, function (n) { return n.x === "18 items to file"; }).length, 0);
    var tree = sampleTree();
    delete tree.sections[1].items[0].items[2].icon;
    var noIcon = tileOf(display.renderTree(tree, local({tab: "actions"})), "actions.delete_empty_tracks");
    assert.strictEqual(findV(noIcon, function (n) { return n.t === "use" && /^#i-delete/.test(n.a.href); }).length, 0);
});

test("a short label falls back on the label", function () {
    var tree = sampleTree();
    delete tree.sections[1].items[0].items[1].short;
    var tile = tileOf(display.renderTree(tree, local({tab: "actions"})), "actions.razor_all");
    assert.strictEqual(textOf(byClass(tile, "deck-t-code")[0]), "Razor all tracks");
});

test("collapsible groups: caption button, count while collapsed, state per group id", function () {
    var open = rendered("files");
    var head = byClass(open, "deck-g-head")[0];
    assert.strictEqual(head.t, "button");
    assert.strictEqual(head.a["data-group"], "files.offline");
    assert.strictEqual(head.a["aria-expanded"], "true");
    assert.strictEqual(textOf(byClass(head, "deck-caption")[0]), "Offline");
    assert.strictEqual(byClass(head, "deck-count").length, 0);
    assert.strictEqual(byClass(open, "deck-list-row").length, 2);
    var closed = rendered("files", {collapsed: {"files.offline": true}});
    var closedHead = byClass(closed, "deck-g-head")[0];
    assert.strictEqual(closedHead.a["aria-expanded"], "false");
    assert.strictEqual(textOf(byClass(closedHead, "deck-count")[0]), "2", "the rows of a list count while collapsed");
    assert.strictEqual(byClass(closed, "deck-list-row").length, 0);
    assert.ok(hasClass(byClass(closed, "deck-group").filter(function (g) { return byClass(g, "deck-g-head").length; })[0], "is-collapsed"));
    // Expanded by default; a group without `collapsible` has a plain caption.
    assert.strictEqual(byClass(rendered("misc"), "deck-g-head").length, 0);
    assert.strictEqual(textOf(byClass(rendered("misc"), "deck-g-title")[0]), "Two columns");
});

test("grid rules: runs of buttons, a text ends a run, a lone plain button is one column", function () {
    var v = rendered("misc");
    var grids = byClass(v, "deck-grid");
    assert.strictEqual(grids.length, 2, "the text splits the buttons into two runs");
    assert.strictEqual(grids[0].a["data-cols"], "2");
    assert.deepStrictEqual(grids[0].c.map(function (c) { return c.a["data-action"]; }), ["misc.g1", "misc.g2", "misc.g3"]);
    assert.strictEqual(grids[1].a["data-cols"], "1", "a run of one plain button is drawn full width");
    assert.ok(findV(v, function (n) { return textOf(n) === "A text breaks the run"; }).length > 0);
    var one = {id: "x", title: "T", collapsed: false, items: [{kind: "group", id: "x.g", title: "G", columns: 1, items: [
        {kind: "button", id: "x.a", label: "A"}, {kind: "button", id: "x.b", label: "B"}]}]};
    var plain = display.renderTree({type: "ui", revision: 1, title: "", status: null, sections: [one]}, local({tab: "x"}));
    assert.strictEqual(byClass(plain, "deck-grid").length, 0, "one column draws no grid");
    var css = fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
    assert.ok(!/@media \(max-width/.test(css), "the stage never follows the panel width");
});

test("confirmation: a tile arms itself (check and cross on its face); a list row and a plain button still ask inline", function () {
    var v = rendered("files", {confirming: "files.sort"});
    var grid = byClass(v, "deck-rowtiles")[0];
    assert.deepStrictEqual(grid.c.map(function (c) { return c.a["data-tile"] || c.a["class"]; }),
        ["files.sort", "files.sync_all", "files.sync_used", "files.zip"], "the tiles keep their order while a tile is armed");
    assert.strictEqual(byClass(v, "deck-confirm").length, 0, "no inline question for a tile");
    var tile = tileOf(v, "files.sort");
    assert.ok(hasClass(tile, "is-armed"));
    assert.ok(!hasClass(tileOf(rendered("files"), "files.sort"), "is-armed"));
    var face = byClass(tile, "deck-armed")[0];
    assert.deepStrictEqual(face.c.map(function (c) { return c.a["class"]; }), ["deck-armed-yes", "deck-armed-q", "deck-armed-no"], "a wide tile: check, question, cross");
    assert.strictEqual(textOf(byClass(face, "deck-armed-q")[0]), "Sort 18 items?");
    assert.strictEqual(face.c[0].a["data-confirm-yes"], "files.sort");
    assert.strictEqual(face.c[0].a["data-action"], "files.sort", "the check sends the button's own id");
    assert.strictEqual(face.c[2].a["data-confirm-no"], "files.sort");
    assert.deepStrictEqual(findV(face.c[0], function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }), ["#i-check"]);
    assert.deepStrictEqual(findV(face.c[2], function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }), ["#i-cross"]);
    assert.strictEqual(face.c[0].p.disabled, false);
    assert.strictEqual(byClass(tileOf(rendered("files"), "files.sort"), "deck-armed")[0].c[0].p.disabled, true, "not pressable while the tile is not armed");
    assert.strictEqual(tile.a["data-tip-label"], "Sort 18 items?", "the tooltip is the question");
    assert.strictEqual(tile.a["data-tip-hint"], "");
    assert.strictEqual(tile.a["data-tip-armed"], "1");
    assert.strictEqual(tileOf(rendered("files"), "files.sort").a["data-tip-armed"], undefined);
    // A lane tile has the two halves only.
    var tree = tree30();
    tree.sections[0].items[0].items[1].confirm = "Cut every track?";
    var laneTile = tileOf(display.renderTree(tree, local({tab: "actions", confirming: "actions.razor_all"})), "actions.razor_all");
    assert.ok(hasClass(laneTile, "is-armed"));
    assert.deepStrictEqual(byClass(laneTile, "deck-armed")[0].c.map(function (c) { return c.a["class"]; }), ["deck-armed-yes", "deck-armed-no"]);
    // A tile without `confirm` has no flipped face; a busy or disabled tile never arms.
    assert.strictEqual(byClass(tileOf(display.renderTree(tree, local({tab: "actions"})), "actions.adjustment_layer"), "deck-armed").length, 0);
    var busy = sampleTree();
    busy.sections.forEach(function (sec) { sec.items.forEach(function (n) { (n.items || []).forEach(function (i) { if (i.id === "files.sort") { i.busy = true; } }); }); });
    assert.ok(!hasClass(tileOf(display.renderTree(busy, local({tab: "files", confirming: "files.sort"})), "files.sort"), "is-armed"), "busy tiles do not arm");
    var list = rendered("misc", {confirming: "misc.open|r2"});
    var rows = byClass(list, "deck-list-row");
    assert.ok(hasClass(rows[1], "is-confirming") && !hasClass(rows[0], "is-confirming"));
    assert.strictEqual(byClass(rows[1], "deck-confirm").length, 1);
    assert.strictEqual(byClass(rows[0], "deck-confirm").length, 0);
    var plainAsk = rendered("misc", {confirming: "misc.collect"});
    assert.strictEqual(byClass(plainAsk, "deck-confirm").length, 1);
    var busyAsk = sampleTree();
    busyAsk.sections[2].items[0].busy = true;
    assert.strictEqual(byClass(display.renderTree(busyAsk, local({tab: "misc", confirming: "misc.collect"})), "deck-confirm").length, 0, "busy buttons do not ask");
});

test("rows: a text beside an icon button is a header, chips sit side by side, a path is mono", function () {
    var v = rendered("files");
    var head = byClass(v, "deck-row")[0];
    assert.ok(hasClass(head, "is-head"));
    var icon = byAction(head, "files.open_folder")[0];
    assert.ok(hasClass(icon, "deck-icon-btn"));
    assert.strictEqual(icon.a["aria-label"], "Open project folder");
    assert.strictEqual(findV(icon, function (n) { return n.t === "use"; })[0].a.href, "#i-open-folder");
    var chips = byClass(v, "deck-row")[1];
    assert.ok(hasClass(chips, "is-chips"));
    assert.deepStrictEqual(byClass(chips, "deck-chip").map(textOf), ["142 in the root", "4 outside", "1 offline"]);
    assert.deepStrictEqual(byClass(chips, "deck-chip").map(function (c) { return hasClass(c, "tone-ok") ? "ok" : hasClass(c, "tone-warn") ? "warn" : "error"; }),
        ["ok", "warn", "error"]);
    assert.strictEqual(byClass(chips, "deck-chip-dot").length, 3);
    var paths = byClass(v, "is-path");
    assert.strictEqual(paths.length, 1, "only the path is mono");
    assert.strictEqual(textOf(paths[0]), "D:\\Footage\\spring-promo");
    assert.ok(!hasClass(byClass(v, "deck-text")[0], "is-path"), "the project name is not");
});

test("a button row followed only by groups sits last in its tab, right under the content and the notice slot", function () {
    var panel = byClass(rendered("files"), "deck-tabpanel")[0];
    var last = panel.c[panel.c.length - 1];
    assert.ok(hasClass(last, "deck-row") && hasClass(last, "is-dock"));
    assert.strictEqual(byAction(last, "files.journal")[0].t, "button");
    assert.ok(hasClass(panel.c[panel.c.length - 2], "deck-notice-box"), "the notice slot comes before the docked row");
    assert.ok(hasClass(panel.c[panel.c.length - 3], "deck-group"), "the Offline group that followed it comes before the slot");
    assert.strictEqual(byClass(rendered("actions"), "is-dock").length, 0);
    var undo = byAction(last, "files.undo_last")[0];
    assert.strictEqual(undo.a["data-tip-label"], "Undo last");
    assert.strictEqual(undo.a["data-tip-hint"], "Undoes the last change.");
    // A row followed by another kind of node stays in place.
    var tree = sampleTree();
    tree.sections[0].items.push({kind: "text", text: "Last line"});
    assert.strictEqual(byClass(display.renderTree(tree, local({tab: "files"})), "is-dock").length, 0);
});

test("progress: label, bar, the current item and a Stop pill that sends the cancel id", function () {
    var v = rendered("files");
    var card = byClass(v, "deck-progress")[0];
    assert.strictEqual(textOf(byClass(card, "deck-progress-label")[0]), "Sorting project 38 / 142");
    assert.strictEqual(byClass(card, "deck-progress-fill")[0].a.style, "width: 27.0%");
    assert.strictEqual(textOf(byClass(card, "deck-progress-detail")[0]), "C0042.MP4 > Footage\\Camera A");
    var stop = byAction(card, "files.stop")[0];
    assert.strictEqual(textOf(stop), "Stop");
    assert.strictEqual(stop.t, "button");
    var offline = display.renderTree(sampleTree(), local({tab: "files", connection: "connecting"}));
    assert.strictEqual(byAction(offline, "files.stop")[0].p.disabled, true);
    var tree = sampleTree();
    delete tree.sections[0].items[4].cancel;
    delete tree.sections[0].items[4].detail;
    var bare = display.renderTree(tree, local({tab: "files"}));
    assert.strictEqual(byClass(bare, "deck-stop").length, 0);
    assert.strictEqual(byClass(bare, "deck-progress-detail").length, 0);
});

test("tooltips: the label and the hint only, data attributes, never a title attribute", function () {
    var v = rendered("actions");
    var tile = bodyOf(v, "actions.adjustment_layer");
    assert.strictEqual(tile.a["data-tip"], "n:actions.adjustment_layer");
    assert.strictEqual(tile.a["data-tip-label"], "Adjustment layer");
    assert.strictEqual(tile.a["data-tip-hint"], "Adds an adjustment layer over the selected clips.");
    // Every tile has a card: the code is short, the name is in the tooltip; without a hint the name alone.
    var empty = bodyOf(v, "actions.delete_empty_tracks");
    assert.strictEqual(empty.a["data-tip-label"], "Delete empty tracks");
    assert.strictEqual(empty.a["data-tip-hint"], "");
    byClass(v, "deck-t-body").forEach(function (b) { assert.ok(b.a["data-tip"], b.a["data-action"]); });
    // Never the code, the meta or a stored value.
    findV(v, function (n) { return !!n.a["data-tip"]; }).forEach(function (n) {
        assert.ok(n.a["data-tip-hint"].indexOf("9:16") < 0 || hasClass(n, "deck-corner"), "a stored value is the corner's tooltip only");
        assert.strictEqual(n.a["data-tip-meta"], undefined);
    });
    assert.strictEqual(byAction(rendered("misc"), "misc.a")[0].a["data-tip"], undefined);
    assert.strictEqual(byAction(rendered("misc"), "misc.collect")[0].a["data-tip-hint"], "Copies the files.");
    assert.strictEqual(byAction(rendered("misc"), "misc.watch")[0].a["data-tip-hint"], undefined, "the input inside carries no tip; the label does");
    var toggle = findV(rendered("misc"), function (n) { return n.t === "label" && n.a["data-tip-hint"] === "Watches the folder."; });
    assert.strictEqual(toggle.length, 1);
    ["files", "actions", "misc"].forEach(function (tab) {
        var attrs = allAttributes(rendered(tab, {confirming: "files.sort", flash: {"actions.razor_all": "error"}, editing: true}));
        assert.strictEqual(attrs.indexOf("title"), -1, "no title attribute in " + tab);
    });
    assert.strictEqual(allAttributes(display.renderTree(tree30(), local({tab: "actions", editing: true}))).indexOf("title"), -1);
    assert.strictEqual(allAttributes(display.renderTree(null, {connection: "connecting"})).indexOf("title"), -1);
    var source = fs.readFileSync(path.join(PANEL, "js", "display.js"), "utf8");
    assert.ok(!/setAttribute\(\s*["']title["']/.test(source) && !/attrs\.title|attrs\["title"\]/.test(source));
    assert.ok(source.indexOf("data-tip-meta") < 0, "the 2.1 meta line of the tooltip is gone");
});

test("the tooltip card follows the data: label in bold over the hint, below when there is no room above", function () {
    var tip = {key: "n:x", label: "Adjustment layer", hint: "Adds a layer.", ready: true, style: "left: 12px; top: 100px", arrow: 40};
    var card = byClass(display.renderTree(sampleTree(), local({tab: "files", tip: tip})), "deck-tip")[0];
    assert.strictEqual(card.a.role, "tooltip");
    assert.deepStrictEqual(card.c.map(function (c) { return c.t; }), ["b", "span", "i"]);
    assert.strictEqual(card.c[0].x, "Adjustment layer");
    assert.strictEqual(card.c[1].x, "Adds a layer.");
    assert.strictEqual(card.c[2].a.style, "left: 40px");
    assert.ok(hasClass(card, "is-ready") && !hasClass(card, "is-below"));
    var onlyHint = byClass(display.renderTree(sampleTree(), local({tab: "files", tip: {key: "k", label: "", hint: "Only a hint."}})), "deck-tip")[0];
    assert.deepStrictEqual(onlyHint.c.map(function (c) { return c.t; }), ["span", "i"]);
    var withMeta = byClass(display.renderTree(sampleTree(), local({tab: "files", tip: {key: "k", label: "Sort project", hint: "Files media.",
        meta: "18 items to file"}})), "deck-tip")[0];
    assert.deepStrictEqual(withMeta.c.map(function (c) { return c.t; }), ["b", "span", "i"], "a meta is never drawn");
    assert.ok(!hasClass(onlyHint, "is-ready"), "measured before it is shown");
    assert.strictEqual(byClass(rendered("files"), "deck-tip").length, 0);
    var stage = byClass(display.renderTree(sampleTree(), local({tab: "files", tip: tip})), "deck-stage")[0];
    assert.strictEqual(byClass(stage, "deck-tip").length, 1, "the card lives on the stage (placed in stage pixels)");
});

test("placeFloating: centred above the element, inside the 12 px gutter, flipped below, end-aligned menus", function () {
    var bounds = {w: 360, h: 640};
    var size = {w: 188, h: 48};
    var middle = display.placeFloating({left: 126, top: 300, width: 107, height: 66}, size, bounds, "center");
    assert.deepStrictEqual([middle.left, middle.top, middle.below], [86, 244, false]);
    assert.strictEqual(middle.arrow, 94, "the nub points at the tile's centre");
    // The first column lines up with the tile's outer edge (the panel's gutter), the last one too.
    var first = display.placeFloating({left: 12, top: 300, width: 107, height: 66}, size, bounds, "center");
    assert.strictEqual(first.left, 12);
    assert.strictEqual(first.arrow, 54);
    var last = display.placeFloating({left: 241, top: 300, width: 107, height: 66}, size, bounds, "center");
    assert.strictEqual(last.left + size.w, 348);
    assert.strictEqual(last.arrow, 135);
    // No room above: the card hangs below the element.
    var top = display.placeFloating({left: 12, top: 40, width: 107, height: 66}, size, bounds, "center");
    assert.deepStrictEqual([top.top, top.below], [114, true]);
    // A narrow panel keeps the card inside.
    var narrow = display.placeFloating({left: 150, top: 300, width: 100, height: 60}, size, {w: 280, h: 640}, "center");
    assert.strictEqual(narrow.left, 80);
    // The menu hangs from the tile's right edge and opens downwards, upwards at the foot of the panel.
    var menu = display.placeFloating({left: 241, top: 300, width: 107, height: 66}, {w: 140, h: 100}, bounds, "end");
    assert.deepStrictEqual([menu.left, menu.top, menu.below], [208, 374, true]);
    var foot = display.placeFloating({left: 241, top: 560, width: 107, height: 66}, {w: 140, h: 100}, bounds, "end");
    assert.deepStrictEqual([foot.top, foot.below], [452, false]);
});

test("results: the tile shows the outcome, the group keeps the line, a detail opens on click", function () {
    var at = new Date(2026, 9, 22, 12, 3, 40).getTime();
    var lines = {"actions.timeline": {outcome: "ok", text: "Adjustment layer added · V3", detail: null, at: at}};
    var v = rendered("actions", {flash: {"actions.adjustment_layer": "ok", "actions.razor_all": "error", "actions.solo_track": "warn",
        "actions.delete_empty_tracks": "neutral"}, lines: lines});
    var classes = ["actions.adjustment_layer", "actions.razor_all", "actions.solo_track", "actions.delete_empty_tracks"].map(function (id) {
        return tileOf(v, id).a["class"];
    });
    assert.ok(/is-done/.test(classes[0]) && /is-failed/.test(classes[1]) && /is-warned/.test(classes[2]) && /is-noted/.test(classes[3]), classes.join(" | "));
    var line = byClass(v, "deck-g-result")[0];
    assert.ok(hasClass(line, "tone-ok") && line.t === "button", "every line is clickable (a click closes it)");
    assert.strictEqual(line.a["data-line"], "actions.timeline");
    assert.ok(!hasClass(line, "has-detail"));
    assert.strictEqual(textOf(byClass(line, "deck-g-result-text")[0]), "Adjustment layer added · V3");
    assert.strictEqual(textOf(byClass(line, "deck-g-result-time")[0]), "12:03");
    assert.strictEqual(display.clock(at), "12:03");
    assert.strictEqual(byClass(line, "more").length, 0);
    // The line sits in the tab's notice slot with its lane's code, in no lane.
    var lanes = byClass(v, "deck-lane-wrap");
    lanes.forEach(function (lane) { assert.strictEqual(byClass(lane, "deck-g-result").length, 0); });
    assert.strictEqual(byClass(byClass(v, "deck-notice-slot")[0], "deck-g-result").length, 1);
    assert.strictEqual(textOf(byClass(line, "deck-g-result-code")[0]), "TIME ·");
    // A failed line with a detail is a button with a chevron; open, the detail follows as one line.
    lines["actions.timeline"] = {outcome: "error", text: "Razor all tracks failed", detail: "Premiere did not cut at the playhead.", at: at};
    var failed = byClass(rendered("actions", {lines: lines}), "deck-g-result")[0];
    assert.ok(failed.t === "button" && hasClass(failed, "has-detail") && hasClass(failed, "tone-error"));
    assert.strictEqual(failed.a["data-line"], "actions.timeline");
    assert.strictEqual(byClass(failed, "more").length, 1);
    // The opened explanation is a card above the slot (the result line closed with the click that opened it).
    var open = rendered("actions", {lines: {}, detailOpen: {"actions.timeline": "Premiere did not cut at the playhead."}});
    assert.strictEqual(byClass(open, "deck-g-result").length, 0);
    var why = byClass(open, "deck-g-why")[0];
    assert.strictEqual(textOf(why), "Premiere did not cut at the playhead.");
    assert.ok(why.t === "button" && why.a["data-why"] === "actions.timeline");
    assert.strictEqual(byClass(rendered("actions", {lines: lines}), "deck-g-why").length, 0);
    // A result of a button outside any group is the section's line, at the foot of the tab.
    var section = rendered("files", {lines: {"~files": {outcome: "warn", text: "Set up this project first", detail: null, at: at}}});
    var panel = byClass(section, "deck-tabpanel")[0];
    var texts = panel.c.map(function (c) { return hasClass(c, "deck-g-result") ? "line" : (hasClass(c, "is-dock") ? "dock" : (hasClass(c, "deck-notice-box") ? "slot" : "-")); });
    assert.deepStrictEqual(texts.slice(-3), ["line", "slot", "dock"], "the docked row stays last, the slot before it");
});

test("action_result routing: the group of the button, else its section, else nothing", function () {
    var tree = sampleTree();
    assert.strictEqual(display.lineKeyFor(tree, "actions.razor_all"), "actions.timeline");
    assert.strictEqual(display.lineKeyFor(tree, "actions.razor_all", "pins"), "~pins", "a press from the pinned row reports there");
    assert.strictEqual(display.lineKeyFor(tree, "files.sort"), "files.actions");
    assert.strictEqual(display.lineKeyFor(tree, "files.journal"), "~files");
    assert.strictEqual(display.lineKeyFor(tree, "misc.open"), "~misc", "a list row button reports to its section");
    assert.strictEqual(display.lineKeyFor(tree, "actions.gone"), null);
    assert.strictEqual(display.lineKeyFor(null, "x"), null);
    assert.strictEqual(display.findButton(tree, "actions.social_copy").node.menu.id, "actions.social_copy.format");
    assert.strictEqual(display.findButton(tree, "actions.social_copy.format").node.id, "actions.social_copy", "a menu id finds its button");
    var keys = display.lineKeys(tree);
    assert.ok(keys["actions.timeline"] && keys["files.actions"] && keys["~files"] && keys["~actions"] && keys["misc.two"] && keys["~pins"]);
    assert.ok(!keys["actions.gone"]);
    assert.strictEqual(display.groupId({title: "Plain"}), "t:Plain");
    assert.strictEqual(display.groupId({id: "a.b", title: "Plain"}), "a.b");
});

test("Display.onResult: 2 s on the tile, the line stays, it goes with its group, unknown ids change nothing", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
        var d = ctx.d;
        d.onState("connected", {});
        d.onMessage(sampleTree());
        d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "error", text: "Razor all tracks failed",
            detail: "Premiere did not cut.", at: 1761000000000});
        assert.strictEqual(d.local.flash["actions.razor_all"], "error");
        assert.strictEqual(d.local.lines["actions.timeline"].text, "Razor all tracks failed");
        assert.ok(/is-failed/.test(domTile(ctx, "actions.razor_all").className));
        assert.ok(find(ctx.container, function (n) { return /deck-g-result/.test(n.className) && n.tagName === "button"; }));
        clock.advance(display.RESULT_MS - 1);
        assert.strictEqual(d.local.flash["actions.razor_all"], "error");
        clock.advance(2);
        assert.strictEqual(d.local.flash["actions.razor_all"], undefined, "the tile returns to its icon after 2 s");
        assert.ok(!/is-failed/.test(domTile(ctx, "actions.razor_all").className));
        assert.ok(d.local.lines["actions.timeline"], "an error line stays 10 s (it outlives the tile's 2 s)");
        // The next result of the group replaces the line; a result for an id that is not in the tree changes nothing.
        d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "ok", text: "Adjustment layer added", detail: null, at: 1761000000000});
        assert.strictEqual(d.local.lines["actions.timeline"].text, "Adjustment layer added");
        assert.strictEqual(d.local.lines["actions.timeline"].detail, null);
        var before = JSON.stringify(d.local.lines);
        d.onMessage({type: "action_result", id: "actions.gone", outcome: "ok", text: "x", detail: null, at: 1});
        assert.strictEqual(JSON.stringify(d.local.lines), before);
        assert.strictEqual(d.local.flash["actions.gone"], undefined);
        // A result for a menu id morphs the button that carries the menu.
        d.onMessage({type: "action_result", id: "actions.social_copy.format", outcome: "ok", text: "Format 4:5", detail: null, at: 1});
        assert.strictEqual(d.local.flash["actions.social_copy"], "ok");
        assert.strictEqual(d.local.lines["actions.sequence"].text, "Format 4:5");
        // A second result restarts the 2 s window.
        d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "ok", text: "Again", detail: null, at: 1});
        clock.advance(1500);
        d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "warn", text: "Again", detail: null, at: 1});
        clock.advance(1500);
        assert.strictEqual(d.local.flash["actions.adjustment_layer"], "warn");
        clock.advance(600);
        assert.strictEqual(d.local.flash["actions.adjustment_layer"], undefined);
        // The group leaves the tree: its line goes with it.
        var next = sampleTree();
        next.sections[1].items.shift();
        d.onMessage(next);
        assert.strictEqual(d.local.lines["actions.timeline"], undefined);
        assert.ok(!find(ctx.container, function (n) { return /deck-g-result/.test(n.className) && n.attributes["data-line"] === "actions.timeline"; }));
    });
});

test("a click on a failed line opens its explanation and closes the line; the explanation closes on its click and on Escape", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
        var why = function () { return find(ctx.container, function (n) { return /deck-g-why/.test(n.className); }); };
        ctx.d.onState("connected", {});
        ctx.d.onMessage(sampleTree());
        ctx.d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "error", text: "Failed", detail: "Because.", at: 1});
        assert.ok(!why());
        clickOn(ctx, byAttr("data-line", "actions.timeline"));
        assert.strictEqual(why().textContent, "Because.");
        assert.ok(!find(ctx.container, byAttr("data-line", "actions.timeline")), "the same click closed the line");
        assert.strictEqual(ctx.d.local.lines["actions.timeline"], undefined);
        clock.advance(display.LINE_ERROR_MS + 1);
        assert.ok(why(), "the explanation has no timer; it stays until its own click");
        clickOn(ctx, byAttr("data-why", "actions.timeline"));
        assert.ok(!why(), "a click on the explanation closes it");
        ctx.d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "error", text: "Failed", detail: "Again.", at: 1});
        clickOn(ctx, byAttr("data-line", "actions.timeline"));
        fire(ctx.container, "keydown", {key: "Escape", target: ctx.container});
        assert.ok(!why(), "Escape closes it");
        // The next result of the group replaces an open explanation.
        ctx.d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "error", text: "Failed", detail: "Third.", at: 1});
        clickOn(ctx, byAttr("data-line", "actions.timeline"));
        ctx.d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "ok", text: "Added", detail: null, at: 1});
        assert.ok(!why());
        assert.ok(ctx.sent.length === 0, "closing notices sends nothing to the host");
    });
});

test("menus: the corner mark on the tile with the current value in its own tooltip, a menu of options", function () {
    var v = rendered("actions");
    var tile = tileOf(v, "actions.social_copy");
    var corner = byClass(tile, "deck-corner")[0];
    assert.strictEqual(corner.t, "button");
    assert.strictEqual(corner.a["data-menu-open"], "actions.social_copy");
    assert.strictEqual(corner.a["data-tip-label"], "Social copy");
    assert.strictEqual(corner.a["data-tip-hint"], "Current: 9:16.", "the corner's tooltip says the current value");
    assert.strictEqual(corner.a["aria-label"], "Social copy: 9:16");
    assert.strictEqual(corner.a.style, undefined, "options without swatches draw the mark in the ink colour");
    var body = byClass(tile, "deck-t-body")[0];
    assert.strictEqual(body.a["data-tip-hint"], "Creates a reframed copy.", "the tile's own tooltip never repeats the value");
    assert.strictEqual(findV(body, function (n) { return n.a["data-menu-open"]; }).length, 0, "the mark is a sibling of the body, not inside it");
    assert.strictEqual(byClass(tileOf(v, "actions.razor_all"), "deck-corner").length, 0);
    assert.strictEqual(byClass(v, "deck-menu").length, 0);
    var menu = byClass(rendered("actions", {menu: {buttonId: "actions.social_copy", ready: true}}), "deck-menu")[0];
    assert.strictEqual(menu.a.role, "menu");
    var items = byClass(menu, "deck-menu-item");
    assert.deepStrictEqual(items.map(textOf), ["9:16", "1:1", "4:5"]);
    assert.deepStrictEqual(items.map(function (i) { return hasClass(i, "is-on"); }), [true, false, false]);
    assert.deepStrictEqual(items.map(function (i) { return i.a["aria-checked"]; }), ["true", "false", "false"]);
    assert.strictEqual(findV(items[0], function (n) { return n.t === "use"; })[0].a.href, "#i-check");
    assert.strictEqual(findV(items[1], function (n) { return n.t === "use"; }).length, 0);
    assert.deepStrictEqual(items.map(function (i) { return i.a["data-menu-pick"]; }), ["9x16", "1x1", "4x5"]);
    assert.ok(items.every(function (i) { return i.a["data-menu-id"] === "actions.social_copy.format"; }));
    // An option's swatch is a dot before its label; the corner mark takes the current option's colour; a value that
    // is not a hex colour draws none.
    var tree = sampleTree();
    var opts = tree.sections[1].items[1].items[1].menu.options;
    opts[0].swatch = "#1aa3e0";
    opts[1].swatch = "red; background-image: url(x)";
    var swatchedV = display.renderTree(tree, local({tab: "actions", menu: {buttonId: "actions.social_copy", ready: true}}));
    var swatched = byClass(swatchedV, "deck-menu")[0];
    var dots = byClass(swatched, "deck-menu-swatch");
    assert.strictEqual(dots.length, 1);
    assert.strictEqual(dots[0].a.style, "background-color: #1aa3e0");
    assert.deepStrictEqual(byClass(swatched, "deck-menu-item")[0].c.map(function (c) { return c.a["class"] || c.t; }),
        ["deck-menu-mark", "deck-menu-swatch", "span"]);
    assert.strictEqual(byClass(tileOf(swatchedV, "actions.social_copy"), "deck-corner")[0].a.style, "--mark: #1aa3e0");
    tree.sections[1].items[1].items[1].menu.value = "1x1";
    assert.strictEqual(byClass(tileOf(display.renderTree(tree, local({tab: "actions"})), "actions.social_copy"), "deck-corner")[0].a.style,
        undefined, "a bad swatch draws no colour");
    // A menu whose button left the tree is not drawn; a long menu takes two columns.
    assert.strictEqual(byClass(rendered("actions", {menu: {buttonId: "actions.gone", ready: true}}), "deck-menu").length, 0);
    var many = sampleTree();
    many.sections[1].items[1].items[1].menu.options = "abcdefghij".split("").map(function (c) { return {value: c, label: c}; });
    assert.ok(hasClass(byClass(display.renderTree(many, local({tab: "actions", menu: {buttonId: "actions.social_copy", ready: true}})), "deck-menu")[0], "is-two"));
});

test("menu flow: a click on the corner opens it, a pick sends the menu id and value, Escape and outside close", function () {
    var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
    var d = ctx.d;
    d.onState("connected", {});
    d.onMessage(sampleTree());
    clickOn(ctx, byAttr("data-menu-open", "actions.social_copy"));
    assert.strictEqual(ctx.sent.length, 0, "the corner does not run the action");
    assert.strictEqual(d.local.menu.buttonId, "actions.social_copy");
    assert.strictEqual(d.local.menu.anchor, "lane:actions.social_copy", "anchored to the tile it was opened from");
    assert.ok(d.local.menu.ready, "measured and placed");
    assert.ok(/^left: \d+px; top: \d+px$/.test(d.local.menu.style), d.local.menu.style);
    var pick = find(ctx.container, byAttr("data-menu-pick", "4x5"));
    assert.ok(pick);
    fire(ctx.container, "click", {target: pick});
    assert.deepStrictEqual(ctx.sent, [{type: "action", id: "actions.social_copy.format", revision: 3, value: "4x5"}]);
    assert.strictEqual(d.local.menu, null);
    assert.ok(!find(ctx.container, function (n) { return /deck-menu/.test(n.className); }));
    // A click on the tile outside the mark sends the tile's own id.
    clickOn(ctx, byAttr("data-action", "actions.social_copy"));
    assert.strictEqual(ctx.sent[1].id, "actions.social_copy");
    assert.strictEqual(ctx.sent[1].value, undefined);
    // Escape closes without sending; so does a click outside, which does nothing else.
    clickOn(ctx, byAttr("data-menu-open", "actions.social_copy"));
    fire(ctx.container, "keydown", {key: "Escape", target: ctx.container});
    assert.strictEqual(d.local.menu, null);
    clickOn(ctx, byAttr("data-menu-open", "actions.social_copy"));
    clickOn(ctx, byAttr("data-action", "actions.razor_all"));
    assert.strictEqual(d.local.menu, null);
    assert.strictEqual(ctx.sent.length, 2, "the closing click ran nothing");
    // A right click on the tile opens the menu too.
    fire(ctx.container, "contextmenu", {target: find(ctx.container, byAttr("data-action", "actions.social_copy")), preventDefault: function () {}});
    assert.strictEqual(d.local.menu.buttonId, "actions.social_copy");
    d.closeMenu();
    // While the host is away the corner opens nothing.
    d.onState("connecting", {});
    clickOn(ctx, byAttr("data-menu-open", "actions.social_copy"));
    assert.strictEqual(d.local.menu, null);
});

test("tooltip flow: 400 ms of hover, gone on leave and on press", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
        var d = ctx.d;
        d.onState("connected", {});
        d.onMessage(sampleTree());
        var tile = find(ctx.container, byAttr("data-action", "actions.adjustment_layer"));
        fire(ctx.container, "mouseover", {target: tile, relatedTarget: null});
        assert.strictEqual(d.local.tip, null);
        clock.advance(display.TIP_DELAY_MS - 1);
        assert.strictEqual(d.local.tip, null, "not before 400 ms");
        clock.advance(2);
        assert.strictEqual(d.local.tip.label, "Adjustment layer");
        assert.strictEqual(d.local.tip.hint, "Adds an adjustment layer over the selected clips.");
        assert.strictEqual(d.local.tip.meta, undefined);
        assert.ok(d.local.tip.ready, "placed after measuring");
        assert.ok(find(ctx.container, function (n) { return /deck-tip/.test(n.className) && n.tagName === "div"; }));
        fire(ctx.container, "mouseout", {target: tile, relatedTarget: null});
        assert.strictEqual(d.local.tip, null);
        fire(ctx.container, "mouseover", {target: tile, relatedTarget: null});
        clock.advance(display.TIP_DELAY_MS + 1);
        assert.ok(d.local.tip);
        fire(ctx.container, "mousedown", {target: tile});
        assert.strictEqual(d.local.tip, null, "hidden on press");
        // Leaving before 400 ms shows nothing.
        fire(ctx.container, "mouseover", {target: tile, relatedTarget: null});
        fire(ctx.container, "mouseout", {target: tile, relatedTarget: null});
        clock.advance(1000);
        assert.strictEqual(d.local.tip, null);
        // An element without a tooltip (a tab) arms nothing.
        fire(ctx.container, "mouseover", {target: find(ctx.container, byAttr("data-tab", "files")), relatedTarget: null});
        assert.strictEqual(clock.pending(), 0);
        // The corner mark has its own card with the current value.
        fire(ctx.container, "mouseover", {target: find(ctx.container, byAttr("data-menu-open", "actions.social_copy")), relatedTarget: null});
        clock.advance(display.TIP_DELAY_MS + 1);
        assert.deepStrictEqual([d.local.tip.label, d.local.tip.hint], ["Social copy", "Current: 9:16."]);
        d.hideTip();
        // A tooltip whose element left the tree is closed by the next render.
        fire(ctx.container, "mouseover", {target: tile, relatedTarget: null});
        clock.advance(500);
        assert.ok(d.local.tip);
        var next = sampleTree();
        next.sections[1].items[0].items.shift();
        d.onMessage(next);
        assert.strictEqual(d.local.tip, null);
    });
});

test("busy, disabled and offline states", function () {
    var v = rendered("misc");
    var busy = byAction(v, "misc.busy")[0];
    assert.ok(hasClass(busy, "is-busy"));
    assert.strictEqual(byClass(busy, "deck-spinner").length, 1);
    assert.strictEqual(byAction(v, "misc.b")[0].p.disabled, true);
    assert.strictEqual(byAction(v, "misc.a")[0].p.disabled, false);
    var tree = sampleTree();
    tree.sections[1].items[0].items[0].busy = true;
    tree.sections[1].items[0].items[1].disabled = true;
    var tiles = display.renderTree(tree, local({tab: "actions"}));
    assert.ok(hasClass(tileOf(tiles, "actions.adjustment_layer"), "is-busy"));
    assert.strictEqual(byClass(tileOf(tiles, "actions.adjustment_layer"), "deck-spinner").length, 1, "the spinner in the icon slot");
    assert.ok(hasClass(tileOf(tiles, "actions.razor_all"), "is-off"));
    assert.ok(hasClass(bodyOf(tiles, "actions.razor_all"), "is-disabled"));
    assert.strictEqual(bodyOf(tiles, "actions.razor_all").p.disabled, true);
    var offline = display.renderTree(sampleTree(), local({tab: "misc", connection: "connecting"}));
    assert.strictEqual(byAction(offline, "misc.a")[0].p.disabled, true);
    assert.strictEqual(byAction(offline, "misc.watch")[0].p.disabled, true);
    assert.ok(hasClass(byClass(offline, "deck-body")[0], "is-offline"), "the last tree stays, dimmed");
    assert.ok(hasClass(byClass(offline, "deck-conn")[0], "tone-warn"), "the dot turns amber");
    assert.strictEqual(byAction(offline, "host.open")[0].p.disabled, true);
    assert.strictEqual(byClass(offline, "deck-connecting").length, 0, "a tree was shown: no connecting screen");
    var tabs = byClass(offline, "deck-tab");
    assert.strictEqual(tabs.length, 3, "the tabs stay");
});

test("the Connecting to Host screen, its second line after 15 s and the start link", function () {
    var first = display.renderTree(null, {connection: "connecting"});
    var screen = byClass(first, "deck-connecting")[0];
    assert.strictEqual(textOf(byClass(screen, "deck-h1")[0]), "Connecting to Host");
    assert.strictEqual(findV(screen, function (n) { return n.t === "use" && n.a.href === "#glyph-pr"; }).length, 1);
    assert.strictEqual(byClass(screen, "deck-pulse").length, 1);
    assert.strictEqual(byClass(screen, "deck-soft").length, 0, "no second line yet");
    assert.strictEqual(findV(first, function (n) { return n.a["data-start-host"] === "1"; }).length, 0);
    assert.strictEqual(byClass(first, "deck-top").length, 0);
    assert.strictEqual(byClass(first, "deck-stage").length, 1, "the screen sits on the stage too");
    var late = display.renderTree(null, {connection: "connecting", late: true});
    assert.strictEqual(textOf(byClass(late, "deck-soft")[0]), "Start Premiere Deck on this computer");
    assert.strictEqual(findV(late, function (n) { return n.a["data-start-host"] === "1"; }).length, 0, "no link without a known exe");
    var link = display.renderTree(null, {connection: "connecting", late: true, hostExe: "C:\\PremiereDeck.exe", startStatus: "Starting Premiere Deck…"});
    var button = findV(link, function (n) { return n.a["data-start-host"] === "1"; })[0];
    assert.strictEqual(textOf(button), "Start Premiere Deck");
    assert.strictEqual(findV(button, function (n) { return n.t === "use"; })[0].a.href, "#i-power");
    assert.strictEqual(byClass(link, "is-small").map(textOf)[0], "Starting Premiere Deck…");
    var protocol = display.renderTree(null, {connection: "protocol", hostVersion: "2.1.0"});
    assert.strictEqual(textOf(byClass(protocol, "deck-h1")[0]), "Update Premiere Deck");
    assert.strictEqual(textOf(byClass(protocol, "deck-soft")[0]), "Host version 2.1.0");
    // The footer keeps the panel's own version, the scale and a disabled Open host while no host is there.
    var footer = findV(first, function (n) { return n.t === "footer"; })[0];
    assert.strictEqual(byAction(footer, "host.open")[0].p.disabled, true);
    assert.strictEqual(byClass(footer, "deck-scale").length, 1);
    assert.strictEqual(textOf(byClass(footer, "deck-ver")[0]), "");
    assert.strictEqual(textOf(byClass(display.renderTree(null, {connection: "connecting", panelVersion: "2.0.0"}), "deck-ver")[0]), "v2.0.0");
});

test("Display: the 15 s line and the start link appear after 15 s without a host", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay();
        var win = ctx.win;
        win.cep = {fs: {stat: function () { return {err: 0}; }, readFile: function (file) {
            return /deck\.json$/.test(file) ? {err: 0, data: JSON.stringify({port: 47800, token: "t", host_exe: "C:\\PremiereDeck.exe", protocol: 1})} : {err: 1};
        }}};
        var seen = {connection: "connecting"};
        ctx.d.common = {readDeck: function () { return {host_exe: "C:\\PremiereDeck.exe"}; }, fileExists: function () { return true; },
            isDevBrowser: function () { return false; }, startHost: function () { return "started"; }};
        ctx.d.armLate();
        ctx.d.render();
        assert.strictEqual(ctx.d.local.late, false);
        clock.advance(display.LATE_MS - 1);
        assert.strictEqual(ctx.d.local.late, false);
        clock.advance(2);
        assert.strictEqual(ctx.d.local.late, true);
        assert.strictEqual(ctx.d.local.hostExe, "C:\\PremiereDeck.exe");
        assert.ok(find(ctx.container, byAttr("data-start-host", "1")));
        // A host that connects clears the line; one that never existed shows no link.
        ctx.d.onState("connected", {});
        assert.strictEqual(ctx.d.local.late, false);
        ctx.d.common.fileExists = function () { return false; };
        ctx.d.onState("connecting", {});
        clock.advance(display.LATE_MS + 1);
        assert.strictEqual(ctx.d.local.hostExe, null);
        assert.ok(seen);
    });
});

test("section result and notices: restart pending, protocol, toast", function () {
    var tree = sampleTree();
    var restart = display.renderTree(tree, local({tab: "files", connection: "restart_pending"}));
    assert.strictEqual(textOf(byClass(restart, "deck-notice")[0]), "Restart Premiere Pro to finish the Premiere Deck update");
    assert.strictEqual(byAction(restart, "files.journal")[0].p.disabled, false, "the panel keeps working");
    assert.strictEqual(display.headerStatus(tree, {connection: "restart_pending"}).text, "Update available · Restart Premiere Pro");
    assert.strictEqual(display.headerStatus(tree, {connection: "protocol", hostVersion: "2.0.0"}).text, "Update Premiere Deck (host 2.0.0)");
    var toast = display.renderTree(tree, local({tab: "files", toast: {text: "Done", tone: "ok"}}));
    assert.strictEqual(findV(toast, function (n) { return n.x === "Done" && hasClass(n, "deck-toast") && hasClass(n, "tone-ok"); }).length, 1);
    var empty = sampleTree();
    empty.sections[2].items = [{kind: "list", id: "l", rows: [], empty: "No results"}];
    assert.strictEqual(findV(display.renderTree(empty, local({tab: "misc"})), function (n) { return n.x === "No results"; }).length, 1);
});

// --- display.js: 2.1 (tab order, switch, timers, click-dismiss) -------------------------------------------------

function tree21() {
    // The 2.1 host order: Actions first, Files second; the Files tab without the Journal tile.
    var files = filesSection();
    files.items = files.items.filter(function (n) { return !(n.kind === "row" && n.items[0].id === "files.journal"); });
    files.items.push({kind: "row", items: [
        {kind: "button", id: "files.undo_last", label: "Undo last", icon: "undo", variant: "icon", hint: "Undoes the last change."}]});
    files.items[3].items.push(
        {kind: "switch", id: "files.auto_copy", label: "Auto copy", short: "COPY", value: true, hint: "Copies imports into the project."},
        {kind: "switch", id: "files.auto_sort", label: "Auto sort", short: "SORT", value: false});
    return sampleTree({sections: [actionsSection(), files]});
}

test("2.1 tab order: the tree's order, the first section is the default, the last tab is remembered", function () {
    var v = display.renderTree(tree21(), local({tab: null}));
    assert.deepStrictEqual(byClass(v, "deck-tab").map(function (t) { return t.a["data-tab"]; }), ["actions", "files"]);
    assert.strictEqual(byClass(v, "deck-tabs")[0].a["data-on"], "actions", "Actions is the default tab");
    var header = findV(v, function (n) { return n.t === "header"; })[0];
    assert.deepStrictEqual(header.c.map(function (c) { return c.a["class"].split(" ")[0]; }), ["deck-glyph", "deck-tabs", "deck-conn"],
        "glyph · tabs · connection dot");
    var ctx = makeDisplay();
    ctx.d.onState("connected", {});
    ctx.d.onMessage(tree21());
    clickOn(ctx, byAttr("data-tab", "files"));
    assert.strictEqual(ctx.win.store["deck.tab"], "files");
    var again = makeDisplay({storage: {"deck.tab": "files"}});
    again.d.onState("connected", {});
    again.d.onMessage(tree21());
    assert.strictEqual(byClass(again.d.vnode, "deck-tabs")[0].a["data-on"], "files", "the last tab comes back");
});

test("the Files tab renders without the Journal tile; the undo row still docks", function () {
    var v = display.renderTree(tree21(), local({tab: "files"}));
    assert.strictEqual(byAction(v, "files.journal").length, 0);
    var panel = byClass(v, "deck-tabpanel")[0];
    var last = panel.c[panel.c.length - 1];
    assert.ok(hasClass(last, "is-dock"));
    assert.strictEqual(byAction(last, "files.undo_last").length, 1);
    assert.deepStrictEqual(byClass(v, "deck-rowtiles")[0].c.map(function (c) { return c.a["data-tile"]; }),
        ["files.sort", "files.sync_all", "files.sync_used", "files.zip", "files.auto_copy", "files.auto_sort"], "switches join the tile grid");
});

test("switch: the whole tile is the switch, a lamp shows the state, role switch on the body", function () {
    var v = rendered("actions");
    var off = tileOf(v, "actions.solo_audio");
    var on = tileOf(v, "actions.lock_others");
    assert.ok(hasClass(off, "deck-t") && hasClass(off, "is-switch") && !hasClass(off, "is-on"));
    assert.ok(hasClass(on, "is-on"));
    var offBody = byClass(off, "deck-t-body")[0];
    var onBody = byClass(on, "deck-t-body")[0];
    assert.strictEqual(offBody.t, "button");
    assert.deepStrictEqual([offBody.a["data-kind"], offBody.a.role, offBody.a["aria-checked"], offBody.a["data-value"]], ["switch", "switch", "false", "false"]);
    assert.deepStrictEqual([onBody.a["aria-checked"], onBody.a["data-value"]], ["true", "true"]);
    assert.deepStrictEqual(off.c.map(function (c) { return c.a["class"]; }), ["deck-t-body", "deck-lamp"], "the lamp sits on the tile");
    assert.deepStrictEqual(offBody.c.map(function (c) { return c.a["class"]; }), ["deck-face", "deck-t-code"], "the icon stays");
    assert.strictEqual(byClass(off, "deck-capsule").length, 0, "no capsule on a switch");
    assert.strictEqual(textOf(byClass(off, "deck-t-code")[0]), "SOLO");
    assert.strictEqual(offBody.a["aria-label"], "Solo audio");
    assert.strictEqual(offBody.a["data-tip-label"], "Solo audio");
    assert.strictEqual(offBody.a["data-tip-hint"], "Mutes every other audio track.");
    assert.strictEqual(onBody.a["data-tip-label"], "Lock other tracks");
    var grid = byClass(byClass(v, "deck-lane-wrap")[4], "deck-lane-tiles")[0];
    assert.deepStrictEqual(grid.c.map(function (c) { return c.a["data-tile"]; }), ["actions.add_video_track", "actions.solo_audio", "actions.lock_others"]);
    // busy greys it and blocks clicks; disabled and offline too; a short label falls back on the label.
    var tree = sampleTree();
    var items = tree.sections[1].items[3].items;
    items[1].busy = true;
    items[2].disabled = true;
    delete items[2].short;
    var states = display.renderTree(tree, local({tab: "actions"}));
    assert.ok(hasClass(tileOf(states, "actions.solo_audio"), "is-busy"));
    assert.ok(hasClass(tileOf(states, "actions.lock_others"), "is-off"));
    assert.strictEqual(bodyOf(states, "actions.lock_others").p.disabled, true);
    assert.strictEqual(textOf(byClass(tileOf(states, "actions.lock_others"), "deck-t-code")[0]), "Lock other tracks");
    var offline = display.renderTree(sampleTree(), local({tab: "actions", connection: "connecting"}));
    assert.strictEqual(bodyOf(offline, "actions.solo_audio").p.disabled, true);
    // A switch outside a tile group is a row tile of the full width.
    var loose = {id: "x", title: "X", collapsed: false, items: [{kind: "switch", id: "x.s", label: "S", value: true}]};
    var lv = display.renderTree({type: "ui", revision: 1, title: "", status: null, sections: [loose]}, local({tab: "x"}));
    assert.ok(hasClass(tileOf(lv, "x.s"), "is-switch") && hasClass(tileOf(lv, "x.s"), "is-on") && hasClass(tileOf(lv, "x.s"), "is-row"));
});

test("switch flow: the tile flips at once, the action asks for the new value, the host's next tree wins", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
        var d = ctx.d;
        var body = function (id) { return find(ctx.container, byAttr("data-action", id)); };
        var tile = function (id) { return domTile(ctx, id); };
        d.onState("connected", {});
        d.onMessage(sampleTree());
        clickOn(ctx, byAttr("data-action", "actions.solo_audio"));
        assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "actions.solo_audio", value: true, revision: 3});
        assert.strictEqual(body("actions.solo_audio").getAttribute("aria-checked"), "true", "optimistic flip before the host answers");
        assert.ok(/is-on/.test(tile("actions.solo_audio").className));
        clickOn(ctx, byAttr("data-action", "actions.lock_others"));
        assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "actions.lock_others", value: false, revision: 3});
        assert.strictEqual(body("actions.lock_others").getAttribute("aria-checked"), "false");
        // The host accepted SOLO and refused LOCK: the tree is the truth, the refusal comes as a result.
        var next = sampleTree({revision: 4});
        next.sections[1].items[3].items[1].value = true;
        d.onMessage(next);
        d.onMessage({type: "action_result", id: "actions.lock_others", outcome: "warn", text: "Lock others needs a selected clip",
            detail: null, at: 1761000000000});
        assert.deepStrictEqual(d.local.pending, {});
        assert.strictEqual(body("actions.solo_audio").getAttribute("aria-checked"), "true");
        assert.strictEqual(body("actions.lock_others").getAttribute("aria-checked"), "true", "the refused change is back at the old value");
        assert.ok(/is-warned/.test(tile("actions.lock_others").className), "the tile glows with the outcome");
        assert.strictEqual(d.local.lines["actions.tracks"].text, "Lock others needs a selected clip");
        clock.advance(display.RESULT_MS + 1);
        assert.ok(!/is-warned/.test(tile("actions.lock_others").className));
        // A tree that comes back unchanged also resets the optimistic value.
        clickOn(ctx, byAttr("data-action", "actions.solo_audio"));
        assert.strictEqual(body("actions.solo_audio").getAttribute("aria-checked"), "false");
        d.onMessage(next);
        assert.strictEqual(body("actions.solo_audio").getAttribute("aria-checked"), "true");
        ctx.sent.length = 0;
        // Busy, disabled and offline switches send nothing and do not flip.
        var busy = sampleTree({revision: 5});
        busy.sections[1].items[3].items[1].busy = true;
        busy.sections[1].items[3].items[2].disabled = true;
        d.onMessage(busy);
        clickOn(ctx, byAttr("data-action", "actions.solo_audio"));
        clickOn(ctx, byAttr("data-action", "actions.lock_others"));
        assert.strictEqual(ctx.sent.length, 0);
        assert.deepStrictEqual(d.local.pending, {});
        d.onMessage(sampleTree());
        d.onState("connecting", {});
        d.flipSwitch("actions.solo_audio", true);
        assert.strictEqual(ctx.sent.length, 0, "nothing while the host is away");
    });
});

test("result line timers: 3 s for ok and neutral, 10 s for warn and error; a new result restarts the timer", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
        var d = ctx.d;
        var line = function () { return find(ctx.container, function (n) { return /deck-g-result/.test(n.className); }); };
        d.onState("connected", {});
        d.onMessage(sampleTree());
        assert.strictEqual(display.LINE_MS, 3000);
        assert.strictEqual(display.LINE_ERROR_MS, 10000);
        [["ok", 3000], ["neutral", 3000], ["warn", 10000], ["error", 10000]].forEach(function (pair) {
            d.onMessage({type: "action_result", id: "actions.razor_all", outcome: pair[0], text: pair[0], detail: null, at: 1});
            clock.advance(pair[1] - 1);
            assert.ok(line(), pair[0] + " still there at " + (pair[1] - 1) + " ms");
            clock.advance(1);
            assert.ok(!line(), pair[0] + " gone at " + pair[1] + " ms");
            assert.strictEqual(d.local.lines["actions.timeline"], undefined);
        });
        d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "ok", text: "first", detail: null, at: 1});
        clock.advance(2500);
        d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "ok", text: "second", detail: null, at: 1});
        clock.advance(2500);
        assert.ok(/second/.test(line().textContent), "the new result of the group restarted the timer");
        clock.advance(501);
        assert.ok(!line());
        // A line whose group leaves the tree takes its timer with it.
        d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "error", text: "x", detail: null, at: 1});
        var next = sampleTree();
        next.sections[1].items.shift();
        d.onMessage(next);
        assert.deepStrictEqual(d.lineTimers, {});
    });
});

test("click-dismiss: result lines, the toast, the restart notice and the connecting hints close on a click", function () {
    withTimers(function (clock) {
        var ctx = makeDisplay({storage: {"deck.tab": "actions"}});
        var d = ctx.d;
        d.onState("connected", {});
        d.onMessage(sampleTree());
        d.onMessage({type: "action_result", id: "actions.razor_all", outcome: "ok", text: "Cut 3 tracks", detail: null, at: 1});
        clickOn(ctx, byAttr("data-line", "actions.timeline"));
        assert.ok(!find(ctx.container, byAttr("data-line", "actions.timeline")));
        assert.deepStrictEqual(d.lineTimers, {}, "its timer is gone too");
        // The toast.
        d.onMessage({type: "toast", tone: "ok", text: "Filed a.mp4", ms: 3500});
        clickOn(ctx, byAttr("data-dismiss", "toast"));
        assert.strictEqual(d.local.toast, null);
        assert.strictEqual(d.toastTimer, null);
        // The restart notice: closed for this page, the panel keeps working.
        d.onState("restart_pending", {});
        assert.ok(find(ctx.container, byAttr("data-dismiss", "restart")));
        clickOn(ctx, byAttr("data-dismiss", "restart"));
        assert.ok(!find(ctx.container, byAttr("data-dismiss", "restart")));
        assert.ok(find(ctx.container, byAttr("data-action", "actions.razor_all")));
        assert.strictEqual(ctx.sent.length, 0, "closing a notice sends nothing");
        // The connecting hints: the soft second line and the start result line.
        var screen = makeDisplay();
        screen.d.common = {readDeck: function () { return {host_exe: "C:\\PremiereDeck.exe"}; }, fileExists: function () { return true; },
            isDevBrowser: function () { return false; }, startHost: function () { return "missing"; }};
        screen.d.armLate();
        screen.d.render();
        clock.advance(display.LATE_MS + 1);
        clickOn(screen, byAttr("data-dismiss", "hint"));
        assert.strictEqual(screen.d.local.hintClosed, true);
        assert.ok(!find(screen.container, byAttr("data-dismiss", "hint")));
        assert.ok(find(screen.container, byAttr("data-start-host", "1")), "the start link is a control, it stays");
        clickOn(screen, byAttr("data-start-host", "1"));
        assert.ok(find(screen.container, byAttr("data-dismiss", "start")));
        clickOn(screen, byAttr("data-dismiss", "start"));
        assert.strictEqual(screen.d.local.startStatus, null);
        screen.d.onState("connected", {});
        assert.strictEqual(screen.d.local.hintClosed, false, "a new connection cycle shows the hint again");
    });
});

test("panel.css: fixed tile shapes, the lamp, the corner mark, the AUTO zone, notices are clickable", function () {
    var css = fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
    var below = css.slice(css.indexOf("/* END generated */"));
    assert.ok(/\.deck-t \{[^}]*width: var\(--tile-w\); height: var\(--tile-h\)/.test(below));
    assert.ok(/\.deck-t\.is-row \{[^}]*width: var\(--row-w\); height: var\(--row-h\)/.test(below));
    assert.ok(/\.deck-t\.is-on \.deck-lamp \{ background: var\(--mint\)/.test(below));
    assert.ok(/\.deck-corner::after \{[^}]*var\(--mark, var\(--ink-3\)\)/.test(below), "the mark takes the swatch colour");
    assert.ok(/\.deck-t\.is-row \.deck-auto \{[^}]*width: 70px/.test(below), "the AUTO zone of a row tile");
    assert.ok(/\.deck-auto\.is-on \.deck-capsule \{ background: var\(--mint\)/.test(below));
    assert.ok(/\.deck-stage \{[^}]*width: var\(--stage-w\);[^}]*margin: 0 auto;/.test(below), "the stage is centred");
    assert.ok(/--stage-w: 360px;/.test(below));
    assert.ok(/\.deck \{[^}]*overflow: auto;/.test(below), "the page scrolls only when the panel is smaller than the stage");
    assert.ok(!/\.deck-toast \{[^}]*pointer-events: none/.test(below), "the toast takes clicks");
    assert.ok(/\.deck-notice \{[^}]*cursor: pointer/.test(below) && /\.deck-soft\.is-notice \{ cursor: pointer; \}/.test(below));
    assert.ok(!/is-meta|deck-tile|deck-badge|deck-status/.test(below), "the 2.x tile, badge and status line are gone");
});

// --- display.js: theme ----------------------------------------------------------------------------------------

test("theme: normaliseTheme and the switch plan", function () {
    assert.strictEqual(display.normaliseTheme("sky"), "sky");
    assert.strictEqual(display.normaliseTheme("black"), "black");
    ["dusk", "", null, undefined, 3, "toString"].forEach(function (name) {
        assert.strictEqual(display.normaliseTheme(name), "black", String(name));
    });
    var plan = display.planThemeSwitch;
    assert.strictEqual(plan({known: false, current: "black", next: "sky", hasViewTransitions: true}), "instant", "the first theme of a page load");
    assert.strictEqual(plan({known: true, current: "black", next: "black", hasViewTransitions: true}), "instant");
    assert.strictEqual(plan({known: true, current: "black", next: "sky", hasViewTransitions: true}), "sweep");
    assert.strictEqual(plan({known: true, current: "black", next: "sky", hasViewTransitions: false}), "fade");
    assert.strictEqual(plan({known: true, current: "black", next: "sky", hasViewTransitions: true, reducedMotion: true}), "instant");
});

test("theme message sets data-theme on <html> and is remembered; an unknown name is black", function () {
    var ctx = makeDisplay({theme: "black"});
    var html = ctx.win.document.documentElement;
    ctx.d.theme = display.normaliseTheme(html.getAttribute("data-theme"));
    ctx.d.onMessage({type: "theme", name: "sky"});
    assert.strictEqual(html.getAttribute("data-theme"), "sky");
    assert.strictEqual(ctx.win.store["deck.theme"], "sky");
    ctx.d.onMessage({type: "theme", name: "dusk"});
    assert.strictEqual(html.getAttribute("data-theme"), "black");
    assert.strictEqual(ctx.win.store["deck.theme"], "black");
    assert.strictEqual(ctx.win.document.transitions, 0, "no View Transitions here");
});

test("theme switch animation: the first message is instant, later ones sweep (View Transitions) or crossfade for 300 ms", function () {
    withTimers(function (clock) {
        var fade = makeDisplay({theme: "black"});
        fade.d.onMessage({type: "theme", name: "sky"});
        assert.ok(!fade.win.classes["theme-fade"], "the first theme of a page load is applied at once");
        fade.d.onMessage({type: "theme", name: "black"});
        assert.ok(fade.win.classes["theme-fade"], "CEF without View Transitions crossfades");
        assert.strictEqual(fade.win.document.documentElement.getAttribute("data-theme"), "black");
        clock.advance(299);
        assert.ok(fade.win.classes["theme-fade"], "the class stays for the 300 ms of the fade");
        clock.advance(100);
        assert.ok(!fade.win.classes["theme-fade"]);
        fade.d.onMessage({type: "theme", name: "black"});
        assert.ok(!fade.win.classes["theme-fade"], "an unchanged theme draws nothing");

        var sweep = makeDisplay({theme: "black", viewTransitions: true});
        sweep.d.onMessage({type: "theme", name: "black"});
        assert.strictEqual(sweep.win.document.transitions, 0);
        sweep.d.onMessage({type: "theme", name: "sky"});
        assert.strictEqual(sweep.win.document.transitions, 1);
        assert.strictEqual(sweep.win.document.documentElement.getAttribute("data-theme"), "sky");
        assert.strictEqual(sweep.win.document.body.childNodes.length, 0, "the bright line is removed when the sweep ends");

        var reduced = makeDisplay({theme: "black", viewTransitions: true, reducedMotion: true});
        reduced.d.onMessage({type: "theme", name: "black"});
        reduced.d.onMessage({type: "theme", name: "sky"});
        assert.strictEqual(reduced.win.document.transitions, 0);
        assert.strictEqual(reduced.win.document.documentElement.getAttribute("data-theme"), "sky");
    });
});

test("the theme is drawn before the host answers: index.html reads the stored name, black without storage", function () {
    var html = fs.readFileSync(path.join(PANEL, "index.html"), "utf8");
    assert.ok(/<html lang="en" data-theme="black">/.test(html));
    assert.ok(/localStorage\.getItem\("deck\.theme"\)/.test(html) && /"sky"/.test(html));
    assert.ok(html.indexOf("Unbounded") === -1 && !/@font-face/.test(html), "the fonts come from panel.css");
    assert.ok(html.indexOf("css/panel.css") > 0 && html.indexOf("js/display.js") > 0);
});

// --- display.js: sources and assets -----------------------------------------------------------------------------

test("tab and collapsed group are remembered in localStorage", function () {
    var ctx = makeDisplay({storage: {"deck.tab": "files", "deck.group.files.offline": "1", "deck.group.actions.export": "0", "other": "x"}});
    assert.strictEqual(ctx.d.local.tab, "files");
    assert.deepStrictEqual(ctx.d.local.collapsed, {"files.offline": true, "actions.export": false});
    ctx.d.onState("connected", {});
    ctx.d.onMessage(sampleTree());
    assert.strictEqual(byClass(ctx.d.vnode, "deck-tabs")[0].a["data-on"], "files");
    assert.strictEqual(find(ctx.container, byAttr("data-group", "files.offline")).getAttribute("aria-expanded"), "false");
    clickOn(ctx, byAttr("data-tab", "actions"));
    assert.strictEqual(ctx.win.store["deck.tab"], "actions");
    assert.strictEqual(ctx.d.local.tab, "actions");
    clickOn(ctx, byAttr("data-tab", "files"));
    clickOn(ctx, byAttr("data-group", "files.offline"));
    assert.strictEqual(ctx.win.store["deck.group.files.offline"], "0");
    assert.ok(find(ctx.container, byAttr("data-action", "files.locate")), "the open group shows its rows");
    clickOn(ctx, byAttr("data-group", "files.offline"));
    assert.strictEqual(ctx.win.store["deck.group.files.offline"], "1");
    assert.ok(!find(ctx.container, byAttr("data-action", "files.locate")));
    // Without storage everything starts as the tree says: first tab, groups expanded.
    var bare = makeDisplay();
    bare.win.localStorage.getItem = function () { throw new Error("blocked"); };
    bare.win.localStorage.setItem = function () { throw new Error("blocked"); };
    bare.d.onState("connected", {});
    bare.d.onMessage(sampleTree());
    clickOn(bare, byAttr("data-tab", "actions"));
    assert.strictEqual(bare.d.local.tab, "actions");
});

test("clicks send the right messages: tiles, confirmation, Stop, footer link, busy and disabled send nothing", function () {
    var ctx = makeDisplay({storage: {"deck.tab": "files"}});
    ctx.d.onState("connected", {});
    ctx.d.onMessage(sampleTree());
    clickOn(ctx, byAttr("data-action", "files.sync_all"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "files.sync_all", revision: 3});
    clickOn(ctx, byAttr("data-action", "files.sort"));
    assert.strictEqual(ctx.sent.length, 0, "a confirmed tile asks first");
    assert.strictEqual(ctx.d.local.confirming, "files.sort");
    clickOn(ctx, byAttr("data-confirm-yes", "files.sort"));
    assert.strictEqual(ctx.sent.pop().id, "files.sort");
    clickOn(ctx, byAttr("data-action", "files.sort"));
    clickOn(ctx, byAttr("data-confirm-no", "files.sort"));
    assert.strictEqual(ctx.sent.length, 0);
    assert.strictEqual(ctx.d.local.confirming, null);
    clickOn(ctx, byAttr("data-action", "files.stop"));
    assert.strictEqual(ctx.sent.pop().id, "files.stop");
    clickOn(ctx, byAttr("data-action", "host.open"));
    assert.strictEqual(ctx.sent.pop().id, "host.open");
    clickOn(ctx, byAttr("data-action", "files.undo_last"));
    assert.strictEqual(ctx.sent.pop().id, "files.undo_last");
    clickOn(ctx, byAttr("data-action", "files.journal"));
    assert.strictEqual(ctx.sent.pop().id, "files.journal");
    var tree = sampleTree();
    tree.sections[0].items[3].items[1].busy = true;
    tree.sections[0].items[3].items[2].disabled = true;
    ctx.d.onMessage(tree);
    clickOn(ctx, byAttr("data-action", "files.sync_all"));
    clickOn(ctx, byAttr("data-action", "files.sync_used"));
    assert.strictEqual(ctx.sent.length, 0);
    ctx.d.onState("connecting", {});
    clickOn(ctx, byAttr("data-action", "files.zip"));
    assert.strictEqual(ctx.sent.length, 0, "controls are disabled while the host is away");
});

test("icons: every symbol the host names exists in the sprite", function () {
    var sprite = fs.readFileSync(path.join(PANEL, "icons.svg"), "utf8");
    var ids = {};
    sprite.replace(/<symbol id="([^"]+)"/g, function (all, id) { ids[id] = true; return all; });
    assert.ok(ids["glyph-pr"]);
    // The icons the panel itself draws.
    ["folder", "actions", "open", "power", "check", "cross", "warn", "info", "chevron", "undo"].forEach(function (name) {
        assert.ok(ids["i-" + name], "i-" + name);
    });
    // The icons the modules name: icon= / icon = in their sources, plus the section icons.
    var src = path.join(ROOT, "src", "premiere_deck", "modules");
    var files = [path.join(src, "files", "module.py"), path.join(src, "quick_actions", "module.py")];
    fs.readdirSync(path.join(src, "quick_actions", "actions")).forEach(function (name) {
        if (/\.py$/.test(name)) {
            files.push(path.join(src, "quick_actions", "actions", name));
        }
    });
    var used = {};
    files.forEach(function (file) {
        fs.readFileSync(file, "utf8").replace(/\bicon\s*=\s*"([a-z0-9-]+)"/g, function (all, name) { used[name] = file; return all; });
    });
    assert.ok(Object.keys(used).length >= 20, "found the icons of the modules: " + Object.keys(used).length);
    Object.keys(used).forEach(function (name) {
        assert.ok(ids["i-" + name], "icon " + name + " (" + path.basename(used[name]) + ") is missing from icons.svg");
    });
    assert.ok(!/stroke="#|fill="#(?!ffffff)/.test(sprite.replace(/<symbol id="glyph-pr"[\s\S]*?<\/symbol>/, "").replace(/<defs>[\s\S]*?<\/defs>/, "")), "icons draw with currentColor");
});

test("panel.css: the classes the renderer emits are styled, nothing newer than Chromium 99, no leftovers", function () {
    var css = fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
    var below = css.slice(css.indexOf("/* END generated */"));
    assert.ok(!/color-mix\(|@container|:has\(|container-type|@layer|text-wrap|\bdvh\b|\bsvh\b/.test(below), "no CSS newer than Chromium 99");
    assert.ok(!/^\s*(translate|rotate|scale)\s*:/m.test(below), "no individual transform properties (Chromium 104)");
    assert.ok(!/Unbounded|--deck-|color-scheme: dark;\n\n    \/\* background/.test(css), "the 1.x tokens are gone");
    assert.ok(/--bg-top:/.test(css) && /\[data-theme="sky"\]/.test(css));
    assert.ok(/@font-face[^}]*Geist Mono/.test(below));
    assert.ok(!/@import|url\(["']?https?:/.test(css), "no imported or remote styles");
    var seen = {};
    ["files", "actions", "misc"].forEach(function (tab) {
        var v = rendered(tab, {confirming: "files.sort", flash: {"actions.razor_all": "error", "actions.adjustment_layer": "ok"}, tip: {key: "k", label: "L", hint: "H"},
            menu: {buttonId: "actions.social_copy", ready: true}, toast: {text: "t", tone: "ok"}, connection: "restart_pending",
            lines: {"actions.timeline": {outcome: "error", text: "t", detail: "d", at: 1}}, detailOpen: {"actions.timeline": "d"},
            pending: {"actions.solo_audio": true}});
        findV(v, function (n) {
            String(n.a["class"] || "").split(" ").forEach(function (name) { if (name) { seen[name] = true; } });
            return false;
        });
    });
    [local({tab: "actions", editing: true, confirming: "pins|files.sort", prompt: {buttonId: "actions.create_custom", ready: true},
        lines: {"~pins": {outcome: "warn", text: "w", detail: null, at: 1}}, flash: {"files.sort": "ok"}}),
        local({tab: "files", menu: {buttonId: "actions.colour", ready: true, above: true}, pending: {"files.auto_sort": true}})].forEach(function (state) {
        var tree = tree30();
        tree.sections[1].items[3].items[2].busy = true;
        tree.sections[1].items[3].items[3].disabled = true;
        findV(display.renderTree(tree, state), function (n) {
            String(n.a["class"] || "").split(" ").forEach(function (name) { if (name) { seen[name] = true; } });
            return false;
        });
    });
    var screen = display.renderTree(null, {connection: "connecting", late: true, hostExe: "x", startStatus: "s"});
    findV(screen, function (n) {
        String(n.a["class"] || "").split(" ").forEach(function (name) { if (name) { seen[name] = true; } });
        return false;
    });
    var unstyled = Object.keys(seen).filter(function (name) {
        return below.indexOf("." + name) < 0;
    });
    assert.deepStrictEqual(unstyled, [], "classes without a CSS rule: " + unstyled.join(", "));
});

test("no source of the panel opens the extension or reads Premiere's skin", function () {
    ["agent.js", "common.js", "display.js"].forEach(function (name) {
        var source = fs.readFileSync(path.join(PANEL, "js", name), "utf8");
        assert.strictEqual(source.indexOf("requestOpenExtension"), -1, name);
        assert.ok(!/appSkinInfo|ThemeColorChanged/.test(name === "common.js" ? source.replace(/appSkinInfo/g, "") : source), name);
    });
    var html = fs.readFileSync(path.join(PANEL, "index.html"), "utf8");
    assert.strictEqual(html.indexOf("requestOpenExtension"), -1);
});

test("common.isDevBrowser is false under Node and inside CEP", function () {
    assert.strictEqual(common.isDevBrowser(), false);
});

// --- display.js: 3.0 Lanes (pinned row, capsule, corner marks, prompt, stage and scale) -------------------------

// The 3.0 tree: group codes, the colour menu with swatches, the custom size as a `prompt`, SORT and SYNC with their
// automation capsule, the pinned row.
function tree30() {
    var actions = {id: "actions", title: "Actions", icon: "actions", status: {text: "Running · x", tone: "accent"}, items: [
        {kind: "group", id: "actions.timeline", title: "Timeline", short: "TIME", tiles: true, collapsible: true, items: [
            {kind: "button", id: "actions.adjustment_layer", label: "Adjustment layer", short: "ADJ", icon: "adjustment-layer", hint: "Adds a layer."},
            {kind: "button", id: "actions.razor_all", label: "Razor all tracks", short: "RZR", icon: "razor-all", hint: "Cuts every track."}
        ]},
        {kind: "group", id: "actions.tracks", title: "Tracks", short: "TRKS", tiles: true, items: [
            {kind: "button", id: "actions.add_video_track", label: "Add video track", short: "+V", icon: "track-video-add"},
            {kind: "switch", id: "actions.solo_track", label: "Solo audio track", short: "SOLO", icon: "solo-track", value: false, hint: "Mutes the others."},
            {kind: "switch", id: "actions.lock_others", label: "Lock other tracks", short: "LOCK", icon: "lock-others", value: true}
        ]},
        {kind: "group", id: "actions.clips", title: "Clips", short: "CLIP", tiles: true, items: [
            {kind: "button", id: "actions.colour", label: "Colour label", short: "CLR", icon: "colour", hint: "Gives the clips the chosen colour.",
                menu: {id: "actions.colour.choice", value: "rose", badge: "Rose", options: [
                    {value: "violet", label: "Violet", swatch: "#a28ae5"}, {value: "rose", label: "Rose", swatch: "#e06a9c"}]}}
        ]},
        {kind: "group", id: "actions.sequence", title: "Sequence", short: "SEQ", tiles: true, items: [
            {kind: "button", id: "actions.duplicate_sequence", label: "Duplicate sequence", short: "DUP", icon: "duplicate-sequence"},
            {kind: "button", id: "actions.create_custom", label: "Reframe custom size", short: "CUST", icon: "ratio-custom", hint: "Creates a copy at the stored size.",
                prompt: {id: "actions.create_custom.size", label: "Custom size", value: "1080x1350", placeholder: "Width x height"}}
        ]}
    ]};
    var files = {id: "files", title: "Files", icon: "folder", status: {text: "Watching · spring", tone: "ok"}, items: [
        {kind: "row", items: [
            {kind: "text", text: "spring-promo_v2"},
            {kind: "button", id: "files.open_folder", label: "Open project folder", icon: "open-folder", variant: "icon", hint: "Opens the folder."}
        ]},
        {kind: "text", text: "D:\\Footage\\spring-promo", small: true},
        {kind: "row", items: [
            {kind: "text", text: "142 in the root", tone: "neutral", small: true, chip: true},
            {kind: "text", text: "4 outside", tone: "warn", small: true, chip: true},
            {kind: "text", text: "1 offline", tone: "error", small: true, chip: true}
        ]},
        {kind: "group", id: "files.actions", title: null, tiles: true, columns: 2, items: [
            {kind: "button", id: "files.sort", label: "Sort project", short: "SORT", icon: "sort", hint: "Files the media.", confirm: "Sort 18 items?",
                auto: {id: "files.auto_sort", label: "Auto sort", hint: "Files new media by itself.", value: false}},
            {kind: "button", id: "files.sync_all", label: "Sync all media", short: "SYNC", icon: "sync-all", hint: "Copies the files into Media.",
                auto: {id: "files.auto_copy", label: "Auto copy", hint: "Copies imports by itself.", value: true}},
            {kind: "button", id: "files.sync_used", label: "Sync used media", short: "USED", icon: "sync-used"},
            {kind: "button", id: "files.zip", label: "Make ZIP", short: "ZIP", icon: "zip"},
            {kind: "button", id: "files.setup", label: "Set up project", short: "SETUP", icon: "folder", tone: "accent"}
        ]},
        {kind: "row", items: [{kind: "button", id: "files.undo_last", label: "Undo last", icon: "undo", variant: "icon", hint: "Undoes: Sorted 18 items."}]},
        {kind: "group", id: "files.offline", title: "Offline", collapsible: true, items: [
            {kind: "list", id: "files.offline_list", empty: "", rows: [{id: "o1", title: "C0042.MP4", subtitle: "Footage\\Camera A", tone: "warn",
                actions: [{kind: "button", id: "files.locate", label: "Locate"}]}]}
        ]}
    ]};
    return {type: "ui", revision: 7, title: "Premiere Deck", status: {text: "Connected · Spring.prproj", tone: "ok"},
        sections: [actions, files], footer: {text: "Premiere Deck 3.0.0", link: {label: "Open host", action: "host.open"}},
        pins: {items: ["actions.adjustment_layer", "actions.colour", "actions.duplicate_sequence", "files.sort"], max: 5}};
}
function connected30(options) {
    var ctx = makeDisplay(Object.assign({storage: {"deck.tab": "actions"}}, options || {}));
    ctx.d.onState("connected", {});
    ctx.d.onMessage(tree30());
    return ctx;
}

test("3.0 lanes: group codes at the edge, the pinned row from `pins` on top, an empty slot per free place, the edit slot", function () {
    var v = display.renderTree(tree30(), local({tab: "actions"}));
    var panel = byClass(v, "deck-tabpanel")[0];
    var pins = panel.c[0];
    assert.ok(hasClass(pins, "is-pins"), "the pinned row comes first");
    assert.strictEqual(pins.a["data-lane"], "~pins");
    assert.deepStrictEqual(findV(byClass(pins, "deck-lane-code")[0], function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }), ["#i-pin"]);
    var cells = byClass(pins, "deck-lane-tiles")[0].c;
    assert.deepStrictEqual(cells.map(function (c) { return c.a["data-tile"] || c.a["class"]; }),
        ["actions.adjustment_layer", "actions.colour", "actions.duplicate_sequence", "files.sort", "deck-slot", "deck-slot is-edit"],
        "four pins, one free place, the edit slot");
    cells.slice(0, 4).forEach(function (c) { assert.strictEqual(c.a["data-zone"], "pins"); });
    // A pinned tile is the same node as a tile: its code, its corner mark, its tooltip; a Files tile pins as a small tile.
    assert.strictEqual(textOf(byClass(cells[1], "deck-t-code")[0]), "CLR");
    assert.strictEqual(byClass(cells[1], "deck-corner")[0].a.style, "--mark: #e06a9c", "the mark in the current label colour");
    assert.strictEqual(byClass(cells[0], "deck-t-body")[0].a["data-tip"], "pin:actions.adjustment_layer", "its own tooltip key");
    assert.ok(hasClass(cells[3], "is-pins"));
    var edit = cells[5];
    assert.strictEqual(edit.t, "button");
    assert.strictEqual(edit.a["data-pins-edit"], "1");
    assert.deepStrictEqual([edit.a["data-tip-label"], edit.a["data-tip-hint"]], ["Edit pinned", "Press tiles below to pin or unpin them; up to 5."]);
    assert.deepStrictEqual(findV(edit, function (n) { return n.t === "use"; }).map(function (n) { return n.a.href; }), ["#i-plus"]);
    assert.strictEqual(byClass(pins, "deck-unpin").length, 0, "no crosses outside edit mode");
    // The lanes below with their codes; the Files tab has no pinned row.
    var codes = byClass(v, "deck-lane-code").slice(1).map(textOf);
    assert.deepStrictEqual(codes, ["TIME", "TRKS", "CLIP", "SEQ"]);
    assert.strictEqual(byClass(display.renderTree(tree30(), local({tab: "files"})), "is-pins").length, 0);
    assert.strictEqual(display.pinsSectionId(tree30()), "actions");
    // An id with no node is skipped (its place is free); `max` sets the places.
    var tree = tree30();
    tree.pins = {items: ["actions.gone", "actions.razor_all"], max: 3};
    var few = byClass(byClass(display.renderTree(tree, local({tab: "actions"})), "is-pins")[0], "deck-lane-tiles")[0].c;
    assert.deepStrictEqual(few.map(function (c) { return c.a["data-tile"] || c.a["class"]; }), ["actions.razor_all", "deck-slot", "deck-slot", "deck-slot is-edit"]);
});

test("a 2.1 tree without `pins` or group codes: an empty pinned row with the edit slot, codes from the titles", function () {
    var v = rendered("actions");
    var pins = byClass(v, "is-pins")[0];
    var cells = byClass(pins, "deck-lane-tiles")[0].c;
    assert.strictEqual(cells.length, display.PINS_MAX + 1);
    assert.ok(cells.slice(0, display.PINS_MAX).every(function (c) { return c.a["class"] === "deck-slot"; }));
    assert.strictEqual(cells[display.PINS_MAX].a["data-pins-edit"], "1");
    assert.deepStrictEqual(byClass(v, "deck-lane-code").slice(1).map(textOf), ["TIME", "SEQU", "EXPO", "TRAC"]);
    // The 2.1 field under the Sequence tiles still renders, at full width under the lane's row.
    var tree = sampleTree();
    tree.sections[1].items[1].items.push({kind: "input", id: "actions.create_custom.size", label: "Custom size", value: "1080x1350",
        placeholder: "Width x height", submit_label: "Create"});
    var lane = byClass(display.renderTree(tree, local({tab: "actions"})), "deck-lane-wrap")[2];
    assert.deepStrictEqual(lane.c.map(function (c) { return c.a["class"].split(" ")[0]; }), ["deck-lane", "deck-node"]);
    assert.strictEqual(byAction(lane, "actions.create_custom.size").length, 1);
});

// --- fix 3.0.1: notice slot, corner mark, popover z-order, slider focus ---------------------------------------------

function panelCss() {
    return fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
}
function cssRule(css, selector) {
    var at = css.indexOf("\n" + selector + " {");
    assert.ok(at >= 0, "a rule for " + selector);
    return css.substring(at + 1, css.indexOf("}", at) + 1);
}
// The child classes and the tile ids of a lane wrapper.
function laneShape(wrap) {
    return {
        children: wrap.c.map(function (c) { return c.a["class"].split(" ")[0]; }),
        tiles: findV(wrap, function (n) { return !!n.a["data-tile"]; }).map(function (n) { return n.a["data-tile"]; })
    };
}

test("notice slot: one per tab directly under the content, present and empty by default, none in a lane", function () {
    ["actions", "files"].forEach(function (tab) {
        var v = display.renderTree(tree30(), local({tab: tab}));
        var slots = byClass(v, "deck-notice-slot");
        assert.strictEqual(slots.length, 1, tab + ": one slot");
        assert.strictEqual(slots[0].c.length, 0, "empty");
        assert.strictEqual(slots[0].a["data-filled"], "0");
        assert.strictEqual(slots[0].a["data-tip"], undefined, "no tooltip while empty");
        // The slot's wrapper is a child of the tab panel right after the last content node (Actions: the last lane;
        // Files: the Offline group), before the docked Undo row; it is not a child of the body, so a taller panel
        // leaves its extra height below it.
        var panel = byClass(v, "deck-tabpanel")[0];
        var at = panel.c.findIndex(function (c) { return hasClass(c, "deck-notice-box"); });
        assert.ok(at > 0, tab + ": the wrapper is in the tab panel");
        assert.ok(findV(panel.c[at], function (n) { return n === slots[0]; }).length === 1);
        var before = panel.c[at - 1];
        if (tab === "actions") {
            assert.ok(hasClass(before, "deck-lane-wrap") && before.a["data-lane"] === "actions.sequence", "right under the last lane");
            assert.strictEqual(panel.c.length - 1, at, "nothing after it");
        } else {
            assert.ok(hasClass(before, "deck-group") && before.c.length > 0 && before.a["class"].indexOf("is-rowtiles") < 0, "right under the Offline group");
            assert.ok(hasClass(panel.c[at + 1], "is-dock") && panel.c.length - 2 === at, "the Undo row follows the slot");
        }
        var body = byClass(v, "deck-body")[0];
        assert.deepStrictEqual(body.c.map(function (c) { return c.a["class"].split(" ")[0]; }), ["deck-tabpanel"], "nothing is anchored to the footer");
    });
    var actions = display.renderTree(tree30(), local({tab: "actions"}));
    byClass(actions, "deck-lane-wrap").forEach(function (wrap) {
        assert.strictEqual(byClass(wrap, "deck-notice-slot").length, 0, "no slot in a lane");
        assert.deepStrictEqual(wrap.c.map(function (c) { return c.a["class"].split(" ")[0]; }), ["deck-lane"]);
    });
    var block = byClass(display.renderTree(tree30(), local({tab: "files"})), "is-rowtiles")[0];
    assert.deepStrictEqual(block.c.map(function (c) { return c.a["class"]; }), ["deck-rowtiles"], "none in the Files block either");
    // A tab without lanes, tile blocks or pins has no slot.
    assert.strictEqual(byClass(rendered("misc"), "deck-notice-slot").length, 0);
    var css = panelCss();
    assert.ok(/--slot-h: \d+px/.test(css), "one fixed height");
    assert.ok(/\.deck-notice-slot \{[^}]*height: var\(--slot-h\)/.test(css), "the slot has a fixed height");
    assert.ok(!/\.deck-notice-slot \{[^}]*(min-height|max-height|display: none|visibility)/.test(css), "the empty slot keeps its place");
});

test("notice slot: a result fills it; the tiles and their order do not change; a result carries its lane's code; it never shows a question", function () {
    var at = 1761000000000;
    var plain = display.renderTree(tree30(), local({tab: "actions"}));
    var base = byClass(plain, "deck-lane-wrap").map(laneShape);
    var slotOf = function (v) { return byClass(v, "deck-notice-slot")[0]; };
    var prefixOf = function (slot) { return textOf(byClass(slot, "deck-g-result-code")[0]); };
    // A result of a lane: "<code> · text" in the one slot, the lanes unchanged.
    var lane = {"actions.tracks": {outcome: "ok", text: "Removed V2", detail: null, at: at}};
    var v = display.renderTree(tree30(), local({tab: "actions", lines: lane}));
    assert.deepStrictEqual(byClass(v, "deck-lane-wrap").map(laneShape), base, "lanes keep their children and tiles while a line shows");
    assert.strictEqual(slotOf(v).a["data-filled"], "1");
    assert.strictEqual(prefixOf(slotOf(v)), "TRKS ·");
    assert.strictEqual(textOf(byClass(slotOf(v), "deck-g-result-text")[0]), "Removed V2");
    assert.strictEqual(slotOf(v).a["data-tip-label"], "TRKS · Removed V2", "the card carries what the slot shows");
    assert.strictEqual(byClass(v, "deck-g-result").length, 1, "the line is nowhere else");
    // The pinned row's line says PIN; a block of row tiles has no prefix.
    var pins = display.renderTree(tree30(), local({tab: "actions", lines: {"~pins": {outcome: "warn", text: "Five pins at most", detail: null, at: at}}}));
    assert.strictEqual(prefixOf(slotOf(pins)), "PIN ·");
    var filesLine = display.renderTree(tree30(), local({tab: "files", lines: {"files.actions": {outcome: "neutral", text: "Nothing to sort", detail: null, at: at}}}));
    assert.strictEqual(byClass(slotOf(filesLine), "deck-g-result-code").length, 0, "no prefix on the Files tab");
    assert.strictEqual(slotOf(filesLine).a["data-tip-label"], "Nothing to sort");
    // Lines of the other tab do not show here; the newest line of the tab wins.
    var other = display.renderTree(tree30(), local({tab: "files", lines: lane}));
    assert.strictEqual(slotOf(other).a["data-filled"], "0");
    var two = display.renderTree(tree30(), local({tab: "actions", lines: {
        "actions.timeline": {outcome: "ok", text: "older", detail: null, at: at, seq: 1},
        "actions.clips": {outcome: "ok", text: "newer", detail: null, at: at, seq: 2},
        "actions.sequence": {outcome: "ok", text: "oldest", detail: null, at: at, seq: 0}}}));
    assert.strictEqual(textOf(byClass(slotOf(two), "deck-g-result-text")[0]), "newer");
    assert.strictEqual(prefixOf(slotOf(two)), "CLIP ·");
    // A question never reaches the slot: an armed tile (lane, pinned, Files) leaves it empty, one line high.
    var tree = tree30();
    tree.sections[0].items[0].items[1].confirm = "Cut every track?";
    [[tree, "actions", "actions.razor_all"], [tree30(), "actions", "pins|files.sort"], [tree30(), "files", "files.sort"]].forEach(function (c) {
        var view = display.renderTree(c[0], local({tab: c[1], confirming: c[2]}));
        assert.strictEqual(slotOf(view).a["data-filled"], "0", c[2] + ": the slot stays empty");
        assert.strictEqual(byClass(view, "deck-confirm").length, 0, c[2] + ": no inline question anywhere");
        assert.ok(!/is-asking/.test(slotOf(view).a["class"]));
    });
    var withLine = display.renderTree(tree, local({tab: "actions", confirming: "actions.razor_all", lines: lane}));
    assert.strictEqual(byClass(slotOf(withLine), "deck-g-result").length, 1, "a showing result stays while a tile is armed");
    var armedLane = byClass(display.renderTree(tree, local({tab: "actions", confirming: "actions.razor_all"})), "deck-lane-wrap").map(laneShape);
    assert.deepStrictEqual(armedLane, byClass(display.renderTree(tree, local({tab: "actions"})), "deck-lane-wrap").map(laneShape), "arming moves no tile");
    var filesBase = laneShape(byClass(display.renderTree(tree30(), local({tab: "files"})), "is-rowtiles")[0]);
    assert.deepStrictEqual(laneShape(byClass(display.renderTree(tree30(), local({tab: "files", confirming: "files.sort"})), "is-rowtiles")[0]), filesBase);
    assert.deepStrictEqual(laneShape(byClass(filesLine, "is-rowtiles")[0]), filesBase);
    assert.deepStrictEqual(filesBase.tiles, ["files.sort", "files.sync_all", "files.sync_used", "files.zip", "files.setup"]);
    // The slot stays one line: no growth rule, no question styles left in it.
    var css = panelCss();
    assert.ok(!/is-asking/.test(css) && !/\.deck-notice-slot[^{]*\.deck-confirm/.test(css));
    assert.ok(/\.deck-notice-slot \{[^}]*height: var\(--slot-h\)/.test(css));
});

test("armed tile flow: a press arms it, the check sends, the cross / an outside press / Esc / 4 s flip it back", function () {
    withTimers(function (clock) {
        var ctx = connected30({storage: {"deck.tab": "files"}});
        var d = ctx.d;
        var sortTile = function () { return find(ctx.container, function (n) { return n.attributes["data-tile"] === "files.sort"; }); };
        var armed = function () { return / is-armed/.test(" " + sortTile().className); };
        var slot = function () { return find(ctx.container, byAttr("data-slot", "files")); };
        assert.strictEqual(display.CONFIRM_MS, 4000);
        assert.ok(!armed());
        // Arming: the press sends nothing; the tile is armed, its tooltip is the question, open at once.
        clickOn(ctx, byAttr("data-action", "files.sort"));
        assert.strictEqual(ctx.sent.length, 0, "a press on a tile with confirm sends nothing");
        assert.strictEqual(d.local.confirming, "files.sort");
        assert.ok(armed());
        assert.strictEqual(d.local.tip.label, "Sort 18 items?", "the tooltip is the question");
        assert.strictEqual(d.local.tip.hint, "");
        assert.ok(d.local.tip.ready, "shown without the 400 ms hover delay");
        assert.ok(find(ctx.container, function (n) { return /deck-tip/.test(n.className) && n.tagName === "div"; }));
        assert.strictEqual(slot().childNodes.length, 0, "the slot shows no question");
        // The tooltip stays while the pointer leaves and re-enters; hovering another element does not replace it.
        fire(ctx.container, "mouseout", {target: sortTile(), relatedTarget: null});
        assert.ok(d.local.tip, "kept while armed");
        fire(ctx.container, "mouseover", {target: find(ctx.container, byAttr("data-action", "files.sync_used")), relatedTarget: null});
        assert.strictEqual(d.local.tip.label, "Sort 18 items?");
        // The check sends the action and flips the tile back.
        clickOn(ctx, byAttr("data-confirm-yes", "files.sort"));
        assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "files.sort", revision: 7});
        assert.ok(!armed() && d.local.confirming === null);
        assert.strictEqual(d.local.tip, null, "the question's tooltip goes with the arming");
        assert.strictEqual(clock.pending(), 0, "the timeout is cancelled");
        // The cross flips it back and sends nothing.
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clickOn(ctx, byAttr("data-confirm-no", "files.sort"));
        assert.ok(!armed());
        assert.strictEqual(ctx.sent.length, 0);
        // A press anywhere else flips it back and does nothing else.
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clickOn(ctx, byAttr("data-action", "files.zip"));
        assert.ok(!armed());
        assert.strictEqual(ctx.sent.length, 0, "the outside press ran nothing");
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clickOn(ctx, byAttr("data-tab", "actions"));
        assert.ok(!armed() || d.local.confirming === null);
        assert.strictEqual(d.local.confirming, null);
        d.selectTab("files");
        // A press on the face between the halves (the question) neither cancels nor sends.
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clickOn(ctx, byAttr("data-armed", "1"));
        assert.ok(armed());
        assert.strictEqual(ctx.sent.length, 0);
        // Esc flips it back (first, before the tooltip's own Esc).
        fire(ctx.container, "keydown", {key: "Escape", target: ctx.container});
        assert.ok(!armed() && d.local.confirming === null && d.local.tip === null);
        // 4 s without a press flips it back.
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clock.advance(display.CONFIRM_MS - 1);
        assert.ok(armed(), "still armed just before 4 s");
        clock.advance(1);
        assert.ok(!armed() && d.local.confirming === null, "flipped back at 4 s");
        assert.strictEqual(ctx.sent.length, 0);
        // A new press restarts the 4 s.
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clock.advance(3000);
        clickOn(ctx, byAttr("data-confirm-no", "files.sort"));
        clickOn(ctx, byAttr("data-action", "files.sort"));
        clock.advance(3000);
        assert.ok(armed(), "the timer restarted with the new arming");
    });
});

test("armed tile flow: pinned tiles and lane tiles arm on their own copy; the check of a pinned tile reports to the pinned row", function () {
    withTimers(function () {
        var ctx = connected30();
        var d = ctx.d;
        var tile = function (id, zone) { return find(ctx.container, function (n) { return n.attributes["data-tile"] === id && n.attributes["data-zone"] === zone; }); };
        press(ctx, domBody(ctx, "files.sort", "pins"));
        assert.strictEqual(d.local.confirming, "pins|files.sort");
        assert.ok(/is-armed/.test(tile("files.sort", "pins").className));
        assert.strictEqual(tile("files.sort", "pins").getAttribute("data-tip-label"), "Sort 18 items?");
        assert.strictEqual(d.local.tip.label, "Sort 18 items?");
        ctx.sent.length = 0;
        clickOn(ctx, byAttr("data-confirm-yes", "pins|files.sort"));
        assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "files.sort", revision: 7});
        d.onMessage({type: "action_result", id: "files.sort", outcome: "ok", text: "Sorted 18 items", detail: null, at: 1});
        assert.strictEqual(d.local.lines["~pins"].text, "Sorted 18 items");
        var slot = find(ctx.container, byAttr("data-slot", "actions"));
        assert.ok(/PIN/.test(slot.textContent) && /Sorted 18 items/.test(slot.textContent));
    });
});

test("menu, popover and detail card are opaque: the tint over the stage colour, in both themes", function () {
    var css = panelCss();
    ["deck-menu, .deck-pop", "deck-g-why.is-card"].forEach(function (sel) {
        var rule = cssRule(css, "." + sel);
        assert.ok(/background-color: var\(--bg-mid\)/.test(rule), sel + ": a solid base");
        assert.ok(/background-image: linear-gradient\(var\(--menu-tint\), var\(--menu-tint\)\)/.test(rule), sel + ": the tint over it");
        assert.ok(!/background: var\(--menu-tint\)/.test(rule));
    });
    // Both themes define the base and a tint that is opaque enough (alpha 0.9 or more) to leave nothing readable below.
    ["black", "sky"].forEach(function (theme) {
        var block = css.substring(css.indexOf(theme === "black" ? ":root, :root[data-theme=\"black\"]" : ":root[data-theme=\"sky\"]"));
        var tint = /--menu-tint: rgba\([^)]*, ([\d.]+)\)/.exec(block);
        assert.ok(tint && Number(tint[1]) >= 0.9, theme + ": --menu-tint alpha");
        assert.ok(/--bg-mid: #[0-9a-f]{6}/i.test(block), theme + ": --bg-mid");
    });
});

test("a long result: cut by the slot, its full text is the slot's tooltip (only when cut), the detail opens as a card above the slot", function () {
    var at = 1761000000000;
    var longText = "Project folders checked, 2 folders created again in the project root";
    var slotOf = function (extra) {
        return byClass(display.renderTree(tree30(), local(Object.assign({tab: "files"}, extra))), "deck-notice-slot")[0];
    };
    var slot = slotOf({lines: {"files.actions": {outcome: "ok", text: longText, detail: null, at: at}}});
    assert.strictEqual(slot.a["data-tip-clip"], "1");
    assert.strictEqual(slot.a["data-tip-label"], longText, "the card carries the full text");
    assert.strictEqual(slot.a["data-tip-hint"], "");
    assert.strictEqual(textOf(byClass(slot, "deck-g-result-text")[0]), longText, "the full text is in the DOM, the stylesheet cuts it");
    var withDetail = slotOf({lines: {"files.actions": {outcome: "error", text: longText, detail: "Because.", at: at}}});
    assert.strictEqual(withDetail.a["data-tip-hint"], "Click to read more.");
    // An open detail is a card above the slot, a sibling of it inside the wrapper, not in a lane.
    var v = display.renderTree(tree30(), local({tab: "files", detailOpen: {"files.actions": "The long explanation of the failure."}}));
    var wrap = byClass(v, "deck-notice-box")[0];
    assert.deepStrictEqual(wrap.c.map(function (c) { return c.a["class"].split(" ")[0]; }), ["deck-g-why", "deck-notice-slot"]);
    assert.ok(hasClass(wrap.c[0], "is-card"));
    assert.strictEqual(textOf(wrap.c[0]), "The long explanation of the failure.");
    assert.strictEqual(byClass(byClass(v, "is-rowtiles")[0], "deck-g-why").length, 0, "not in the tile block");
    var css = panelCss();
    assert.ok(/\.deck-g-why\.is-card \{[^}]*position: absolute[^}]*bottom: calc\(100% \+ 4px\)/.test(css), "above the slot");
    assert.ok(/\.deck-g-result-text \{[^}]*overflow: hidden; text-overflow: ellipsis; white-space: nowrap/.test(css));
    // The card shows only when the line is really cut.
    withTimers(function (clock) {
        var ctx = connected30({storage: {"deck.tab": "files"}});
        var d = ctx.d;
        d.onMessage({type: "action_result", id: "files.setup", outcome: "warn", text: longText, detail: "More.", at: 1});
        var text = find(ctx.container, function (n) { return /deck-g-result-text/.test(n.className); });
        fire(ctx.container, "mouseover", {target: text, relatedTarget: null});
        clock.advance(display.TIP_DELAY_MS + 1);
        assert.strictEqual(d.local.tip, null, "not cut: no card");
        fire(ctx.container, "mouseout", {target: text, relatedTarget: null});
        text.scrollWidth = 400;
        text.clientWidth = 250;
        fire(ctx.container, "mouseover", {target: text, relatedTarget: null});
        clock.advance(display.TIP_DELAY_MS + 1);
        assert.strictEqual(d.local.tip.label, longText, "cut: the card shows the full text");
        fire(ctx.container, "mouseout", {target: text, relatedTarget: null});
        // The first click closes the line and opens the detail as a card above the slot; its click closes it.
        clickOn(ctx, byAttr("data-line", "files.actions"));
        var card = find(ctx.container, byAttr("data-why", "files.actions"));
        assert.strictEqual(card.textContent, "More.");
        assert.ok(/deck-notice-box/.test(card.parentNode.className) && !/deck-notice-slot/.test(card.parentNode.className));
        assert.strictEqual(find(ctx.container, byAttr("data-slot", "files")).childNodes.length, 0, "the slot is empty again");
        clickOn(ctx, byAttr("data-why", "files.actions"));
        assert.ok(!find(ctx.container, byAttr("data-why", "files.actions")));
    });
});

test("corner mark: a clipped 6 px triangle in the mark colour, no border trick, no rotation, no radius", function () {
    var v = display.renderTree(tree30(), local({tab: "actions"}));
    ["actions.colour", "actions.create_custom"].forEach(function (id) {
        var corner = byClass(tileOf(v, id), "deck-corner")[0];
        assert.strictEqual(corner.t, "button");
        assert.strictEqual(corner.c.length, 0, "no inner element: the triangle is the ::after of the button");
        assert.ok(!/rotate|transform|border/.test(corner.a.style || ""), "no inline shape");
    });
    var css = panelCss();
    var rule = cssRule(css, ".deck-corner::after");
    assert.ok(/width: 6px; height: 6px/.test(rule));
    assert.ok(/background: var\(--mark, var\(--ink-3\)\)/.test(rule), "the swatch colour (--mark) or the ink colour");
    assert.ok(/clip-path: polygon\(0 0, 100% 0, 100% 100%\)/.test(rule));
    assert.ok(!/border|radius|rotate|transform/.test(rule), "CEF 99 draws a border triangle with a radius as a square");
    assert.strictEqual(byClass(tileOf(v, "actions.colour"), "deck-corner")[0].a.style, "--mark: #e06a9c", "CLR keeps its swatch colour");
    assert.strictEqual(byClass(tileOf(v, "actions.create_custom"), "deck-corner")[0].a.style, undefined, "CUST uses the ink colour");
});

test("menu and popover paint over every lane: children of the stage, z-index above everything but the theme sweep", function () {
    var css = panelCss();
    var rule = cssRule(css, ".deck-menu, .deck-pop");
    var z = Number(/z-index: (\d+)/.exec(rule)[1]);
    var rest = css.substring(css.indexOf("/* END generated */"));
    var others = [];
    rest.replace(/([^{}]+)\{([^}]*)\}/g, function (all, selector, body) {
        var m = /z-index: (-?\d+)/.exec(body);
        if (m && !/\.deck-menu|\.theme-line/.test(selector) && !/^\s*(from|to|\d+%)/.test(selector)) {
            others.push([selector.trim(), Number(m[1])]);
        }
        return all;
    });
    assert.ok(others.length > 5, "the stylesheet's other layers were found");
    others.forEach(function (pair) {
        assert.ok(z > pair[1], pair[0] + " (" + pair[1] + ") stays below the menu and the popover (" + z + ")");
    });
    assert.ok(/\.deck-body \{[^}]*position: relative;[^}]*z-index: 0/.test(css), "the body is a stacking context: no lane layer climbs above the cards");
    // The cards are children of the stage, after the body, never inside a lane.
    var ctx = connected30();
    clickOn(ctx, byAttr("data-prompt-open", "actions.create_custom"));
    var pop = find(ctx.container, function (n) { return /deck-pop\b/.test(n.className); });
    assert.ok(/deck-stage/.test(pop.parentNode.className));
    var siblings = pop.parentNode.childNodes;
    var body = siblings.filter(function (n) { return /deck-body/.test(n.className); })[0];
    assert.ok(siblings.indexOf(pop) > siblings.indexOf(body), "after the body in the stage");
    fire(ctx.container, "keydown", {key: "Escape", target: ctx.container});
    clickOn(ctx, byAttr("data-menu-open", "actions.colour"));
    var menu = find(ctx.container, function (n) { return /deck-menu\b/.test(n.className); });
    assert.ok(menu && /deck-stage/.test(menu.parentNode.className), "the colour menu is a child of the stage as well");
});

test("scale slider focus: a round ring on the thumb, no rectangular outline, keyboard focus stays visible", function () {
    var css = panelCss();
    assert.ok(/\.deck-scale-input:focus, \.deck-scale-input:focus-visible \{ outline: none; \}/.test(css));
    var ring = cssRule(css, ".deck-scale-input:focus-visible::-webkit-slider-thumb");
    assert.ok(/box-shadow:[^;]*0 0 0 2px var\(--focus-ring\)/.test(ring), "the ring follows the round thumb");
    assert.ok(!/:has\(|color-mix\(/.test(css.replace(/\/\*[\s\S]*?\*\//g, "")), "CEF 99: no :has() and no color-mix()");
});

test("armed tile styling: a 220 ms flip with transform and opacity only, the accent rim, check and cross halves", function () {
    var css = panelCss();
    var face = cssRule(css, ".deck-armed");
    assert.ok(/transform: perspective\(\d+px\) rotateX\(-90deg\)/.test(face) && /transition: transform 220ms/.test(face), "flipped away at rest, 220 ms");
    assert.ok(/opacity: 0/.test(face) && /pointer-events: none/.test(face), "unarmed: invisible and not pressable");
    assert.ok(/\.deck-t\.is-armed \.deck-armed \{[^}]*opacity: 1[^}]*transform: none/.test(css), "armed: flipped in");
    assert.ok(/\.deck-t\.is-armed \.deck-t-body \{[^}]*rotateX\(90deg\)/.test(css), "the plain face flips out");
    assert.ok(/\.deck-t\.is-armed::after \{[^}]*var\(--accent\)/.test(css), "the accent rim of the pressed state");
    assert.ok(/\.deck-armed-yes \{[^}]*var\(--ok-ink\)/.test(css) && /\.deck-armed-no \{/.test(css));
    assert.ok(/\.deck-armed-q \{[^}]*white-space: nowrap/.test(css));
    assert.ok(/\.deck-t\.is-armed \.deck-auto, \.deck-t\.is-armed \.deck-corner, \.deck-t\.is-armed \.deck-lamp \{[^}]*opacity: 0/.test(css), "the capsule, mark and lamp do not show through the flipped face");
    assert.ok(!/(^|[\s;{])(rotate|scale|translate):/.test(css.replace(/\/\*[\s\S]*?\*\//g, "")), "no individual transform properties (CEF 99)");
    // The question drawn on a wide tile is hidden when it does not fit one line.
    var ctx = connected30({storage: {"deck.tab": "files"}});
    var q = find(ctx.container, function (n) { return /deck-armed-q/.test(n.className); });
    q.scrollWidth = 90;
    q.clientWidth = 60;
    ctx.d.fitQuestions();
    assert.strictEqual(q.style.visibility, "hidden");
    q.scrollWidth = 60;
    ctx.d.fitQuestions();
    assert.strictEqual(q.style.visibility, "visible");
});

test("text fields turn the spell checker and autocomplete off", function () {
    var v = rendered("misc");
    var inputs = findV(v, function (n) { return n.t === "input" && n.a.type === "text"; });
    assert.ok(inputs.length > 0);
    inputs.forEach(function (n) { assert.deepStrictEqual([n.a.spellcheck, n.a.autocomplete], ["false", "off"]); });
    var pop = byClass(display.renderTree(tree30(), local({tab: "actions", prompt: {buttonId: "actions.create_custom", ready: true}})), "deck-pop-field")[0];
    assert.deepStrictEqual([pop.a.spellcheck, pop.a.autocomplete], ["false", "off"]);
});

test("pinned row edit mode: + opens it, a press below pins or unpins, every pinned tile has a cross, the check ends it", function () {
    var ctx = connected30();
    var d = ctx.d;
    clickOn(ctx, byAttr("data-pins-edit", "1"));
    assert.strictEqual(d.local.editing, true);
    assert.strictEqual(ctx.sent.length, 0, "entering edit mode sends nothing");
    var panel = byClass(d.vnode, "deck-tabpanel")[0];
    assert.ok(hasClass(panel, "is-editing"));
    var crosses = byClass(byClass(d.vnode, "is-pins")[0], "deck-unpin");
    assert.deepStrictEqual(crosses.map(function (c) { return c.a["data-unpin"]; }),
        ["actions.adjustment_layer", "actions.colour", "actions.duplicate_sequence", "files.sort"], "every pinned tile shows its cross");
    assert.strictEqual(crosses[0].a["aria-label"], "Unpin Adjustment layer");
    var edit = find(ctx.container, byAttr("data-pins-edit", "1"));
    assert.strictEqual(edit.getAttribute("data-tip-label"), "Done");
    assert.ok(/is-on/.test(edit.className));
    // The tiles below are pinnable; the pinned ones are marked.
    assert.ok(hasClass(tileOf(d.vnode, "actions.razor_all"), "is-pinnable") && !hasClass(tileOf(d.vnode, "actions.razor_all"), "is-pinned"));
    assert.ok(hasClass(tileOf(d.vnode, "actions.adjustment_layer"), "is-pinned"));
    // A press on a free tile below pins it at once and asks the host.
    clickOn(ctx, byAttr("data-anchor", "lane:actions.razor_all"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "panel.pin", value: "actions.razor_all", revision: 7});
    assert.ok(domTile(ctx, "actions.razor_all", "pins"), "the row shows the pin before the host answers");
    assert.ok(!find(ctx.container, function (n) { return n.attributes["class"] === "deck-slot"; }), "the free place is taken");
    // The row is full: a press on another tile asks the host, which refuses it on the row's line.
    clickOn(ctx, byAttr("data-action", "actions.add_video_track"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "panel.pin", value: "actions.add_video_track", revision: 7});
    assert.ok(!domTile(ctx, "actions.add_video_track", "pins"));
    d.onMessage({type: "action_result", id: "panel.pins", outcome: "warn", text: "Five pins at most", detail: null, at: 1});
    assert.strictEqual(d.local.lines["~pins"].text, "Five pins at most");
    assert.ok(find(ctx.container, byAttr("data-line", "~pins")), "the line sits under the pinned row");
    // A press below on a pinned tile unpins it; so does the cross of a pinned tile; the pressed tiles run nothing.
    clickOn(ctx, byAttr("data-anchor", "lane:actions.colour"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "panel.unpin", value: "actions.colour", revision: 7});
    clickOn(ctx, byAttr("data-unpin", "files.sort"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "panel.unpin", value: "files.sort", revision: 7});
    assert.deepStrictEqual(d.local.pinsPending, ["actions.adjustment_layer", "actions.duplicate_sequence", "actions.razor_all"]);
    // In edit mode the pinned tiles answer only their cross, and a corner mark below pins instead of opening.
    clickOn(ctx, byAttr("data-anchor", "pins:actions.adjustment_layer"));
    clickOn(ctx, byAttr("data-menu-open", "actions.colour"));
    assert.strictEqual(d.local.menu, null);
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "panel.pin", value: "actions.colour", revision: 7});
    assert.strictEqual(ctx.sent.length, 0);
    // The host's next tree is the truth for the row.
    var next = tree30();
    next.revision = 8;
    next.pins.items = ["actions.duplicate_sequence", "actions.razor_all"];
    d.onMessage(next);
    assert.strictEqual(d.local.pinsPending, null);
    assert.deepStrictEqual(byClass(byClass(d.vnode, "is-pins")[0], "deck-unpin").map(function (c) { return c.a["data-unpin"]; }),
        ["actions.duplicate_sequence", "actions.razor_all"]);
    // The check ends edit mode; so do Escape, a tab switch and a lost host.
    clickOn(ctx, byAttr("data-pins-edit", "1"));
    assert.strictEqual(d.local.editing, false);
    assert.strictEqual(byClass(d.vnode, "deck-unpin").length, 0);
    clickOn(ctx, byAttr("data-pins-edit", "1"));
    fire(ctx.container, "keydown", {key: "Escape", target: ctx.container});
    assert.strictEqual(d.local.editing, false);
    clickOn(ctx, byAttr("data-pins-edit", "1"));
    clickOn(ctx, byAttr("data-tab", "files"));
    assert.strictEqual(d.local.editing, false);
    clickOn(ctx, byAttr("data-tab", "actions"));
    clickOn(ctx, byAttr("data-pins-edit", "1"));
    d.onState("connecting", {});
    assert.strictEqual(d.local.editing, false);
    d.setEditing(true);
    assert.strictEqual(d.local.editing, false, "no edit mode while the host is away");
});

test("pinned tiles run their actions outside edit mode; their result line sits under the pinned row", function () {
    withTimers(function () {
        var ctx = connected30();
        var d = ctx.d;
        press(ctx, domBody(ctx, "actions.adjustment_layer", "pins"));
        assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "actions.adjustment_layer", revision: 7});
        d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "ok", text: "Added on V3", detail: null, at: 1});
        assert.strictEqual(d.local.lines["~pins"].text, "Added on V3", "pressed in the pinned row: the line under that row");
        assert.ok(/is-done/.test(domTile(ctx, "actions.adjustment_layer", "pins").className));
        assert.ok(/is-done/.test(domTile(ctx, "actions.adjustment_layer").className), "both copies morph");
        // Pressed in its lane: the lane's line.
        press(ctx, domBody(ctx, "actions.adjustment_layer"));
        d.onMessage({type: "action_result", id: "actions.adjustment_layer", outcome: "ok", text: "Again", detail: null, at: 1});
        assert.strictEqual(d.local.lines["actions.timeline"].text, "Again");
        // A pinned tile with a confirmation arms its own copy; the check sends its id.
        press(ctx, domBody(ctx, "files.sort", "pins"));
        assert.strictEqual(d.local.confirming, "pins|files.sort");
        assert.ok(/is-armed/.test(domTile(ctx, "files.sort", "pins").className));
        ctx.sent.length = 0;
        clickOn(ctx, byAttr("data-confirm-yes", "pins|files.sort"));
        assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "files.sort", revision: 7});
        // A pinned menu opens from its own corner, anchored to the pinned copy.
        clickOn(ctx, function (n) { return n.attributes["data-menu-open"] === "actions.colour" && n.parentNode.attributes["data-zone"] === "pins"; });
        assert.strictEqual(d.local.menu.anchor, "pins:actions.colour");
        assert.ok(d.local.menu.ready);
    });
});

test("the AUTO capsule: its own target and tooltip, flips at once, sends the auto id with the new value, usable while busy", function () {
    var v = display.renderTree(tree30(), local({tab: "files"}));
    var sort = tileOf(v, "files.sort");
    assert.ok(hasClass(sort, "has-auto") && hasClass(sort, "is-row"));
    var cap = byClass(sort, "deck-auto")[0];
    assert.strictEqual(cap.t, "button");
    assert.deepStrictEqual([cap.a["data-action"], cap.a["data-kind"], cap.a.role, cap.a["aria-checked"], cap.a["data-value"]],
        ["files.auto_sort", "auto", "switch", "false", "false"]);
    assert.deepStrictEqual([cap.a["data-tip-label"], cap.a["data-tip-hint"]], ["Auto sort", "Files new media by itself."]);
    assert.deepStrictEqual(cap.c.map(function (c) { return c.a["class"]; }), ["deck-auto-word", "deck-capsule"]);
    assert.strictEqual(textOf(byClass(cap, "deck-auto-word")[0]), "AUTO");
    assert.ok(hasClass(byClass(tileOf(v, "files.sync_all"), "deck-auto")[0], "is-on"));
    assert.strictEqual(byClass(tileOf(v, "files.zip"), "deck-auto").length, 0);
    assert.deepStrictEqual(byClass(v, "deck-rowtiles")[0].c.map(function (c) { return textOf(byClass(c, "deck-t-code")[0]); }),
        ["SORT", "SYNC", "USED", "ZIP", "SETUP"], "the 2 x 3 row tiles of the Files tab");
    var ctx = connected30({storage: {"deck.tab": "files"}});
    var d = ctx.d;
    clickOn(ctx, byAttr("data-action", "files.auto_copy"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "files.auto_copy", value: false, revision: 7});
    assert.strictEqual(find(ctx.container, byAttr("data-action", "files.auto_copy")).getAttribute("aria-checked"), "false", "optimistic");
    assert.strictEqual(ctx.sent.length, 0, "the capsule never runs the tile's own action");
    // The body still runs the tile; a busy or disabled tile blocks its body only.
    clickOn(ctx, byAttr("data-action", "files.sync_all"));
    assert.strictEqual(ctx.sent.pop().id, "files.sync_all");
    var busy = tree30();
    busy.sections[1].items[3].items[1].busy = true;
    busy.sections[1].items[3].items[0].disabled = true;
    d.onMessage(busy);
    clickOn(ctx, byAttr("data-action", "files.sync_all"));
    assert.strictEqual(ctx.sent.length, 0);
    clickOn(ctx, byAttr("data-action", "files.auto_copy"));
    clickOn(ctx, byAttr("data-action", "files.auto_sort"));
    assert.deepStrictEqual(ctx.sent.map(function (m) { return [m.id, m.value]; }), [["files.auto_copy", false], ["files.auto_sort", true]]);
    // The host's next tree wins; offline the capsule sends nothing.
    d.onMessage(tree30());
    assert.strictEqual(find(ctx.container, byAttr("data-action", "files.auto_copy")).getAttribute("aria-checked"), "true");
    ctx.sent.length = 0;
    d.onState("connecting", {});
    clickOn(ctx, byAttr("data-action", "files.auto_copy"));
    assert.strictEqual(ctx.sent.length, 0);
});

test("prompt: the corner mark opens a popover with one field; Enter or Apply sends the prompt id and the text", function () {
    var v = display.renderTree(tree30(), local({tab: "actions"}));
    var corner = byClass(tileOf(v, "actions.create_custom"), "deck-corner")[0];
    assert.strictEqual(corner.a["data-prompt-open"], "actions.create_custom");
    assert.deepStrictEqual([corner.a["data-tip-label"], corner.a["data-tip-hint"]], ["Custom size", "Current: 1080x1350."]);
    assert.strictEqual(bodyOf(v, "actions.create_custom").a["data-tip-hint"], "Creates a copy at the stored size.");
    var ctx = connected30();
    var d = ctx.d;
    clickOn(ctx, byAttr("data-prompt-open", "actions.create_custom"));
    assert.strictEqual(ctx.sent.length, 0, "opening sends nothing");
    assert.strictEqual(d.local.prompt.buttonId, "actions.create_custom");
    assert.ok(d.local.prompt.ready, "placed in stage pixels");
    var pop = byClass(d.vnode, "deck-pop")[0];
    assert.deepStrictEqual(pop.c.map(function (c) { return c.t; }), ["input", "button"], "one field and its button");
    assert.strictEqual(pop.c[0].p.value, "1080x1350");
    assert.strictEqual(pop.c[0].a.placeholder, "Width x height");
    assert.strictEqual(textOf(pop.c[1]), "Apply");
    var field = find(ctx.container, byAttr("data-prompt-field", "actions.create_custom.size"));
    field.value = "1080x1920";
    clickOn(ctx, byAttr("data-prompt-submit", "actions.create_custom.size"));
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "actions.create_custom.size", value: "1080x1920", revision: 7});
    assert.strictEqual(d.local.prompt, null);
    // Enter submits; Escape and a click outside close without sending.
    clickOn(ctx, byAttr("data-prompt-open", "actions.create_custom"));
    field = find(ctx.container, byAttr("data-prompt-field", "actions.create_custom.size"));
    field.value = "720x720";
    fire(ctx.container, "keydown", {key: "Enter", target: field, preventDefault: function () {}});
    assert.deepStrictEqual(ctx.sent.pop(), {type: "action", id: "actions.create_custom.size", value: "720x720", revision: 7});
    clickOn(ctx, byAttr("data-prompt-open", "actions.create_custom"));
    fire(ctx.container, "keydown", {key: "Escape", target: ctx.container});
    assert.strictEqual(d.local.prompt, null);
    clickOn(ctx, byAttr("data-prompt-open", "actions.create_custom"));
    clickOn(ctx, byAttr("data-action", "actions.razor_all"));
    assert.strictEqual(d.local.prompt, null);
    assert.strictEqual(ctx.sent.length, 0, "the closing click ran nothing");
    // The tile's body runs its own action; the answer is the button's result.
    clickOn(ctx, byAttr("data-action", "actions.create_custom"));
    assert.strictEqual(ctx.sent.pop().id, "actions.create_custom");
    d.onMessage({type: "action_result", id: "actions.create_custom", outcome: "ok", text: "Created Spring 1080x1920", detail: null, at: 1});
    assert.strictEqual(d.local.lines["actions.sequence"].text, "Created Spring 1080x1920");
    // A popover whose button left the tree is not drawn.
    assert.strictEqual(byClass(display.renderTree(tree30(), local({tab: "actions", prompt: {buttonId: "actions.gone", ready: true}})), "deck-pop").length, 0);
});

test("scale: the footer slider 80..150 % in steps of 5, the 125 % first start on a 1.25 screen, CSS zoom on the stage, stored", function () {
    assert.strictEqual(display.clampScale(97), 95);
    assert.strictEqual(display.clampScale(10), 80);
    assert.strictEqual(display.clampScale(400), 150);
    assert.strictEqual(display.clampScale("abc"), 100);
    assert.strictEqual(display.initialScale(null, 1), 100);
    assert.strictEqual(display.initialScale(null, 1.25), 125);
    assert.strictEqual(display.initialScale(null, 1.5), 125);
    assert.strictEqual(display.initialScale("110", 1.5), 110, "a stored value wins");
    assert.strictEqual(display.initialScale("x", 1), 100);
    var v = display.renderTree(tree30(), local({tab: "actions", scale: 125}));
    var input = findV(v, function (n) { return n.a["data-scale"] === "1"; })[0];
    assert.deepStrictEqual([input.a.type, input.a.min, input.a.max, input.a.step, input.p.value], ["range", "80", "150", "5", "125"]);
    assert.strictEqual(input.a["aria-label"], "Panel scale");
    assert.strictEqual(input.a.style, "--p: 64.29%", "the filled part of the track");
    assert.strictEqual(textOf(byClass(v, "deck-scale-value")[0]), "125 %");
    assert.strictEqual(byClass(v, "deck-scale")[0].a["data-tip-label"], "Panel scale");
    assert.strictEqual(byClass(v, "deck-stage")[0].a.style, "zoom: 1.25; min-height: 640px");
    var ctx = makeDisplay({pixelRatio: 1.25});
    assert.strictEqual(ctx.d.local.scale, 125, "the first start on a 125 % screen");
    assert.strictEqual(ctx.win.store["deck.scale"], undefined, "the pick is not stored until the slider moves");
    var stored = makeDisplay({pixelRatio: 1.5, storage: {"deck.scale": "90"}});
    assert.strictEqual(stored.d.local.scale, 90);
    stored.d.onState("connected", {});
    stored.d.onMessage(tree30());
    var range = find(stored.container, byAttr("data-scale", "1"));
    range.value = "135";
    fire(stored.container, "input", {target: range});
    assert.strictEqual(stored.d.local.scale, 135);
    assert.strictEqual(stored.win.store["deck.scale"], "135");
    assert.strictEqual(find(stored.container, function (n) { return /deck-stage/.test(n.className); }).getAttribute("style"),
        "zoom: 1.35; min-height: 640px");
    assert.strictEqual(stored.sent.length, 0, "the scale is the panel's own; nothing goes to the host");
    assert.ok(/zoom/.test(fs.readFileSync(path.join(PANEL, "js", "display.js"), "utf8")));
});

test("stage: 360 x 640, taller with a taller panel, floating cards placed in stage pixels", function () {
    assert.strictEqual(display.STAGE_W, 360);
    assert.strictEqual(display.STAGE_H, 640);
    assert.strictEqual(display.stageStyle({scale: 100, viewHeight: 800}), "zoom: 1; min-height: 800px", "the body grows with a taller panel");
    assert.strictEqual(display.stageStyle({scale: 125, viewHeight: 800}), "zoom: 1.25; min-height: 640px", "800 px at 125 % is the stage's 640");
    assert.strictEqual(display.stageStyle({scale: 100, viewHeight: 480}), "zoom: 1; min-height: 640px", "a shorter panel scrolls");
    assert.strictEqual(display.stageStyle({scale: 80, viewHeight: 1000}), "zoom: 0.8; min-height: 1250px");
    // An element's rectangle in stage pixels, whether the engine reports zoomed rectangles or not.
    assert.deepStrictEqual(display.stageRect({left: 150, top: 250, width: 115, height: 55}, {left: 50, top: 0, width: 450, height: 800}),
        {left: 80, top: 200, width: 92, height: 44});
    assert.deepStrictEqual(display.stageRect({left: 130, top: 200, width: 92, height: 44}, {left: 50, top: 0, width: 360, height: 640}),
        {left: 80, top: 200, width: 92, height: 44});
    var ctx = makeDisplay({innerHeight: 900});
    ctx.d.onState("connected", {});
    ctx.d.onMessage(tree30());
    assert.strictEqual(byClass(ctx.d.vnode, "deck-stage")[0].a.style, "zoom: 1; min-height: 900px");
    var manifest = fs.readFileSync(path.join(PANEL, "CSXS", "manifest.xml"), "utf8");
    var panel = manifest.slice(manifest.indexOf("com.softoblivion.premieredeck.panel\""));
    assert.ok(/<Size>\s*<Height>640<\/Height>\s*<Width>360<\/Width>\s*<\/Size>/.test(panel), "default 360 x 640");
    assert.ok(/<MinSize>\s*<Height>480<\/Height>\s*<Width>320<\/Width>\s*<\/MinSize>/.test(panel), "minimum 320 x 480");
});

test("panel.css: every text names a type role, no pixel font size below the generated block", function () {
    var css = fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
    var block = css.slice(css.indexOf("/* BEGIN generated"), css.indexOf("/* END generated */"));
    var below = css.slice(css.indexOf("/* END generated */"));
    ["display", "title", "section", "body", "label", "label-s", "caption", "mono", "mono-s"].forEach(function (role) {
        assert.ok(new RegExp("--t-" + role + ": ").test(block), "role " + role + " in the generated block");
    });
    var hits = below.split("\n").filter(function (line) {
        return /font-size\s*:\s*\d+(\.\d+)?px/.test(line) || /font\s*:\s*(?!var\(--t-)[^;]*\d+(\.\d+)?px/.test(line);
    });
    assert.deepStrictEqual(hits, []);
    // The role mapping: tile codes label-s, group codes section, result lines caption, the version mono-s.
    assert.ok(/\.deck-t-code \{[^}]*font: var\(--t-label-s\)/.test(below));
    assert.ok(/\.deck-lane-code \{[^}]*font: var\(--t-section\)/.test(below));
    assert.ok(/\.deck-g-result \{[^}]*font: var\(--t-caption\)/.test(below));
    assert.ok(/\.deck-ver \{ font: var\(--t-mono-s\)/.test(below));
});


// --- display.js: diff and applyOps ----------------------------------------------------------------------------

test("diff of identical trees is empty", function () {
    var a = rendered("misc");
    var b = rendered("misc");
    assert.deepStrictEqual(display.diff(a, b), []);
});

test("diff emits text, attrs and props operations", function () {
    var a = rendered("misc");
    var tree = sampleTree();
    tree.sections[2].items[5] = {kind: "text", text: "Changed", tone: "error"};
    tree.sections[2].items[2] = {kind: "toggle", id: "misc.watch", label: "Watch", value: false, hint: "Watches the folder."};
    var ops = display.diff(a, display.renderTree(tree, local({tab: "misc"})));
    var kinds = ops.map(function (op) { return op.op; });
    assert.ok(kinds.indexOf("text") >= 0);
    assert.ok(kinds.indexOf("attrs") >= 0);
    var props = ops.filter(function (op) { return op.op === "props"; });
    assert.strictEqual(props.length, 1);
    assert.deepStrictEqual(props[0].set, {checked: false});
    assert.strictEqual(ops.filter(function (op) { return op.op === "replace"; }).length, 0);
});

test("a select's value is set after its options change", function () {
    var a = rendered("misc");
    var tree = sampleTree();
    tree.sections[2].items[3] = {kind: "select", id: "misc.layout", label: "Layout", value: "c",
        options: [{value: "a", label: "A"}, {value: "c", label: "C"}]};
    var ops = display.diff(a, display.renderTree(tree, local({tab: "misc"})));
    var childrenAt = -1;
    var propsAt = -1;
    ops.forEach(function (op, index) {
        if (op.op === "children" && op.order.indexOf("option|o:c") >= 0) {
            childrenAt = index;
        }
        if (op.op === "props" && op.set.value === "c") {
            propsAt = index;
        }
    });
    assert.ok(childrenAt >= 0 && propsAt > childrenAt, "props after children");
});

test("sprite icons are created in the SVG namespace", function () {
    var doc = fakeDocument();
    var container = doc.createElement("div");
    display.applyOps(container, display.diff(null, rendered("files")));
    var uses = findAll(container, function (n) { return n.tagName === "use"; });
    assert.ok(uses.length > 5);
    assert.ok(uses.every(function (n) { return n.namespace === "http://www.w3.org/2000/svg"; }));
    assert.ok(findAll(container, function (n) { return n.tagName === "svg"; }).every(function (n) { return n.namespace === "http://www.w3.org/2000/svg"; }));
    assert.ok(findAll(container, function (n) { return n.tagName === "button"; }).every(function (n) { return n.namespace === null; }));
});

test("applyOps keeps elements by id across tab switches, reorder, insert and removal", function () {
    var doc = fakeDocument();
    var container = doc.createElement("div");
    var first = rendered("misc");
    display.applyOps(container, display.diff(null, first));
    var input = find(container, function (n) { return n.attributes["data-action"] === "misc.find"; });
    var list = find(container, function (n) { return n.attributes["data-list"] === "misc.results"; });
    assert.ok(input && list);
    input.value = "typed by the user";

    var tree = sampleTree();
    var items = tree.sections[2].items;
    var listNode = items.pop();
    listNode.rows.reverse();
    listNode.rows.push({id: "r3", title: "Three", actions: []});
    items.unshift(listNode);
    items.splice(items.indexOf(items.filter(function (n) { return n.id === "misc.busy"; })[0]), 1);
    var second = display.renderTree(tree, local({tab: "misc"}));
    display.applyOps(container, display.diff(first, second));

    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "misc.find"; }), input);
    assert.strictEqual(input.value, "typed by the user", "an unchanged host value does not overwrite the user's text");
    assert.strictEqual(find(container, function (n) { return n.attributes["data-list"] === "misc.results"; }), list);
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "misc.busy"; }), null);
    var titles = [];
    find(list, function (n) {
        if (n.attributes["class"] === "deck-list-title") {
            titles.push(n.textContent);
        }
        return false;
    });
    assert.deepStrictEqual(titles, ["Two", "One", "Three"]);
    var panel = find(container, function (n) { return n.attributes["class"] === "deck-tabpanel"; });
    assert.strictEqual(panel.childNodes[0].attributes["data-list"], "misc.results");

    // A changed host value is applied.
    var third = sampleTree();
    third.sections[2].items[4] = {kind: "input", id: "misc.find", label: "Find", value: "", placeholder: "Search"};
    var thirdV = display.renderTree(third, local({tab: "misc"}));
    display.applyOps(container, display.diff(second, thirdV));
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "misc.find"; }).value, "");
});

test("a tile keeps its element while its result morphs and the pinned row changes", function () {
    var doc = fakeDocument();
    var container = doc.createElement("div");
    var first = rendered("actions");
    display.applyOps(container, display.diff(null, first));
    var tile = find(container, function (n) { return n.attributes["data-anchor"] === "lane:actions.razor_all"; });
    var second = rendered("actions", {flash: {"actions.razor_all": "ok"}});
    display.applyOps(container, display.diff(first, second));
    assert.strictEqual(find(container, function (n) { return n.attributes["data-anchor"] === "lane:actions.razor_all"; }), tile, "the morph is a class change");
    assert.ok(/is-done/.test(tile.attributes["class"]));
    var pinned = sampleTree({pins: {items: ["actions.razor_all"], max: 5}});
    var third = display.renderTree(pinned, local({tab: "actions"}));
    display.applyOps(container, display.diff(second, third));
    assert.strictEqual(find(container, function (n) { return n.attributes["data-anchor"] === "lane:actions.razor_all"; }), tile,
        "pinning adds a copy and keeps the lane tile");
    assert.ok(find(container, function (n) { return n.attributes["data-anchor"] === "pins:actions.razor_all"; }));
    var fourth = rendered("actions");
    display.applyOps(container, display.diff(third, fourth));
    var fresh = doc.createElement("div");
    display.applyOps(fresh, display.diff(null, fourth));
    assert.deepStrictEqual(serialise(container), serialise(fresh));
});

test("applyOps result equals a fresh render", function () {
    var doc = fakeDocument();
    var patched = doc.createElement("div");
    var a = rendered("files");
    display.applyOps(patched, display.diff(null, a));
    var tree = sampleTree({title: "Other"});
    tree.sections.reverse();
    var b = display.renderTree(tree, local({tab: "actions", confirming: "files.collect", toast: {text: "t", tone: "ok"},
        lines: {"actions.timeline": {outcome: "ok", text: "x", detail: null, at: 1}}}));
    display.applyOps(patched, display.diff(a, b));
    var fresh = doc.createElement("div");
    display.applyOps(fresh, display.diff(null, b));
    assert.deepStrictEqual(serialise(patched), serialise(fresh));
    // And back to the connecting screen and again.
    var screen = display.renderTree(null, {connection: "connecting", late: true, hostExe: "x"});
    display.applyOps(patched, display.diff(b, screen));
    var freshScreen = doc.createElement("div");
    display.applyOps(freshScreen, display.diff(null, screen));
    assert.deepStrictEqual(serialise(patched), serialise(freshScreen));
});

// --- ExtendScript sources ---------------------------------------------------------------------------------------

test("every jsx compiles with lib prepended", function () {
    var lib = fs.readFileSync(path.join(JSX, "lib.jsx"), "utf8");
    fs.readdirSync(JSX).filter(function (name) { return /\.jsx$/.test(name); }).forEach(function (name) {
        var text = fs.readFileSync(path.join(JSX, name), "utf8");
        var program = /\/\/ @uses lib/.test(text.split("\n").slice(0, 3).join("\n")) ? lib + "\n" + text : text;
        try {
            // eslint-disable-next-line no-new-func
            new Function("__args", program);
        } catch (e) {
            assert.fail(name + ": " + e.message);
        }
    });
});

test("host.jsx compiles and defines the helpers", function () {
    var host = fs.readFileSync(path.join(PANEL, "host.jsx"), "utf8");
    // eslint-disable-next-line no-new-func
    new Function(host);
    ["__deckError", "__deckEncode", "__deckCheckSyntax", "__deckReady", "__deckState", "__deckBind", "__deckDispatch"].forEach(function (name) {
        assert.ok(host.indexOf("function " + name + "(") >= 0, name);
    });
    assert.ok(!/\b(alert|confirm|prompt)\(|\$\.writeln/.test(host.replace(/^\/\/.*$/gm, "")));
});

// --- runner ---------------------------------------------------------------------------------------------------

var failed = 0;
tests.forEach(function (t) {
    try {
        t.fn();
        console.log("ok   " + t.name);
    } catch (e) {
        failed++;
        console.log("FAIL " + t.name + "\n     " + (e && e.stack ? e.stack.split("\n").slice(0, 4).join("\n     ") : e));
    }
});
console.log("\n" + (tests.length - failed) + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
