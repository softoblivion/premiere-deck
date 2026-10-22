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

// --- a minimal DOM for applyOps -------------------------------------------------------------------------------

function FakeNode(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag;
    this.nodeType = 1;
    this.attributes = {};
    this.childNodes = [];
    this.parentNode = null;
    this._text = "";
}
Object.defineProperty(FakeNode.prototype, "firstChild", {get: function () { return this.childNodes[0] || null; }});
Object.defineProperty(FakeNode.prototype, "nextSibling", {get: function () {
    if (!this.parentNode) {
        return null;
    }
    var list = this.parentNode.childNodes;
    return list[list.indexOf(this) + 1] || null;
}});
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
function fakeDocument() {
    var doc = {createElement: function (tag) { return new FakeNode(doc, tag); }};
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

// --- fixtures -------------------------------------------------------------------------------------------------

function sampleTree(overrides) {
    var tree = {
        type: "ui", revision: 3, title: "Premiere Deck", status: {text: "Connected · A.prproj", tone: "ok"},
        sections: [
            {id: "files", title: "Files", collapsed: false, status: {text: "Watching", tone: "ok"}, items: [
                {kind: "button", id: "files.collect", label: "Collect", tone: "accent", busy: false, confirm: "Copy 14 files?"},
                {kind: "button", id: "files.busy", label: "Busy", busy: true},
                {kind: "toggle", id: "files.watch", label: "Watch", value: true},
                {kind: "select", id: "files.layout", label: "Layout", value: "b", options: [{value: "a", label: "A"}, {value: "b", label: "B"}]},
                {kind: "input", id: "files.find", label: "Find", value: "car", placeholder: "Search", submit_label: "Go"},
                {kind: "text", text: "Hello", tone: "warn", small: true},
                {kind: "progress", value: 0.25, label: "Copying"},
                {kind: "progress", value: null},
                {kind: "row", items: [{kind: "button", id: "files.a", label: "A"}, {kind: "button", id: "files.b", label: "B", disabled: true}]},
                {kind: "divider"},
                {kind: "list", id: "files.results", empty: "Nothing", rows: [
                    {id: "r1", title: "One", subtitle: "sub", tone: "ok", thumb: null, actions: [{kind: "button", id: "files.open", label: "Open"}]},
                    {id: "r2", title: "Two", subtitle: "", tone: "neutral", thumb: "data:image/png;base64,AA==", actions: [{kind: "button", id: "files.open", label: "Open", confirm: "Sure?"}]}
                ]}
            ]},
            {id: "actions", title: "Quick actions", collapsed: true, items: [{kind: "text", text: "hidden"}]}
        ],
        footer: {text: "Premiere Deck 0.1.0", link: {label: "Open host", action: "host.open"}}
    };
    return Object.assign(tree, overrides || {});
}
var CONNECTED = {connection: "connected", collapsed: {}, confirming: null};

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

test("renderTree renders every node kind", function () {
    var v = display.renderTree(sampleTree(), CONNECTED);
    ["files.collect", "files.busy", "files.watch", "files.layout", "files.find", "files.a", "files.b", "host.open"].forEach(function (id) {
        assert.strictEqual(byAction(v, id).length, 1, id);
    });
    assert.strictEqual(byAction(v, "files.open").length, 2);
    assert.strictEqual(findV(v, function (n) { return n.t === "hr"; }).length, 1);
    assert.strictEqual(findV(v, function (n) { return n.a["class"] === "deck-progress-fill" && n.a.style === "width: 25.0%"; }).length, 1);
    assert.strictEqual(findV(v, function (n) { return /is-indeterminate/.test(n.a["class"] || ""); }).length, 1);
    assert.strictEqual(findV(v, function (n) { return n.t === "img"; }).length, 1);
    var text = findV(v, function (n) { return n.x === "Hello"; })[0];
    assert.ok(/tone-warn/.test(text.a["class"]) && /is-small/.test(text.a["class"]));
    assert.strictEqual(byAction(v, "files.watch")[0].p.checked, true);
    assert.strictEqual(byAction(v, "files.layout")[0].p.value, "b");
    assert.strictEqual(byAction(v, "files.find")[0].p.value, "car");
    assert.strictEqual(findV(v, function (n) { return n.a["data-submit"] === "files.find"; }).length, 1);
    assert.strictEqual(findV(v, function (n) { return n.x === "Connected · A.prproj"; }).length, 1);
});

test("busy, disabled and offline states", function () {
    var v = display.renderTree(sampleTree(), CONNECTED);
    var busy = byAction(v, "files.busy")[0];
    assert.ok(/is-busy/.test(busy.a["class"]));
    assert.strictEqual(findV(busy, function (n) { return n.a["class"] === "deck-spinner"; }).length, 1);
    assert.strictEqual(byAction(v, "files.b")[0].p.disabled, true);
    assert.strictEqual(byAction(v, "files.a")[0].p.disabled, false);
    var offline = display.renderTree(sampleTree(), {connection: "connecting", collapsed: {}});
    assert.strictEqual(byAction(offline, "files.a")[0].p.disabled, true);
    assert.strictEqual(byAction(offline, "files.watch")[0].p.disabled, true);
    assert.strictEqual(findV(offline, function (n) { return n.x === "Connecting to Premiere Deck…"; }).length, 1);
});

test("sections collapse from the host value unless the user chose", function () {
    var v = display.renderTree(sampleTree(), CONNECTED);
    assert.strictEqual(findV(v, function (n) { return n.x === "hidden"; }).length, 0);
    var opened = display.renderTree(sampleTree(), {connection: "connected", collapsed: {actions: false, files: true}});
    assert.strictEqual(findV(opened, function (n) { return n.x === "hidden"; }).length, 1);
    assert.strictEqual(byAction(opened, "files.collect").length, 0);
});

test("inline confirmation opens only for its button and row", function () {
    var closed = display.renderTree(sampleTree(), CONNECTED);
    assert.strictEqual(findV(closed, function (n) { return n.a["class"] === "deck-confirm"; }).length, 0);
    assert.strictEqual(byAction(closed, "files.collect")[0].a["data-confirm"], "files.collect");
    var open = display.renderTree(sampleTree(), {connection: "connected", collapsed: {}, confirming: "files.collect"});
    var confirm = findV(open, function (n) { return n.a["class"] === "deck-confirm"; });
    assert.strictEqual(confirm.length, 1);
    assert.strictEqual(findV(confirm[0], function (n) { return n.x === "Copy 14 files?"; }).length, 1);
    var row = display.renderTree(sampleTree(), {connection: "connected", collapsed: {}, confirming: "files.open|r2"});
    var yes = findV(row, function (n) { return n.a["data-confirm-yes"] === "files.open|r2"; });
    assert.strictEqual(yes.length, 1);
    assert.strictEqual(yes[0].a["data-row"], "r2");
});

test("list row bar is a thin bar under the subtitle", function () {
    var tree = sampleTree();
    tree.sections[0].items = [{kind: "list", id: "hits", empty: "", rows: [
        {id: "a", title: "A", subtitle: "00:01:12", tone: "neutral", thumb: null, bar: 0.5, actions: []},
        {id: "b", title: "B", subtitle: "", tone: "neutral", thumb: null, bar: 1.4, actions: []},
        {id: "c", title: "C", subtitle: "x", tone: "neutral", thumb: null, bar: null, actions: []},
        {id: "d", title: "D", subtitle: "y", tone: "neutral", thumb: null, actions: []}
    ]}];
    var v = display.renderTree(tree, {connection: "connected", collapsed: {}});
    var bars = findV(v, function (n) { return n.a["class"] === "deck-list-bar"; });
    assert.strictEqual(bars.length, 2, "only rows with a numeric bar draw one");
    var fills = findV(v, function (n) { return n.a["class"] === "deck-list-bar-fill"; });
    assert.deepStrictEqual(fills.map(function (n) { return n.a.style; }), ["width: 50.0%", "width: 100.0%"]);
    var text = findV(v, function (n) { return n.a["class"] === "deck-list-text"; })[0];
    var kinds = text.c.map(function (n) { return n.a["class"]; });
    assert.deepStrictEqual(kinds, ["deck-list-title", "deck-list-subtitle", "deck-list-bar"]);
});

function groupTree(items) {
    var tree = sampleTree();
    tree.sections = [{id: "actions", title: "Quick actions", collapsed: false, items: items}];
    return tree;
}
function btn(id, extra) {
    return Object.assign({kind: "button", id: id, label: id}, extra || {});
}

test("group renders its caption and lays consecutive buttons out in two columns", function () {
    var v = display.renderTree(groupTree([
        {kind: "group", title: "Timeline", columns: 2, items: [
            btn("a.one"), btn("a.two"), btn("a.three"),
            {kind: "select", id: "a.sel", label: "Preset", value: "x", options: [{value: "x", label: "X"}]},
            btn("a.four"),
            {kind: "text", text: "note", small: true},
            btn("a.five"), btn("a.six")
        ]}
    ]), CONNECTED);
    var groups = findV(v, function (n) { return /(^| )deck-group( |$)/.test(n.a["class"] || ""); });
    assert.strictEqual(groups.length, 1);
    var caption = findV(groups[0], function (n) { return n.a["class"] === "deck-group-title"; });
    assert.strictEqual(caption.length, 1);
    assert.strictEqual(caption[0].x, "Timeline");
    var items = findV(groups[0], function (n) { return n.a["class"] === "deck-group-items"; })[0];
    // Runs: [one two three] grid, select, [four] grid, text, [five six] grid; nothing but runs is wrapped.
    assert.deepStrictEqual(items.c.map(function (n) { return /deck-grid/.test(n.a["class"] || "") ? "grid" : n.k; }),
        ["grid", "n:a.sel", "grid", "p:5:text", "grid"]);
    var grids = items.c.filter(function (n) { return /deck-grid/.test(n.a["class"] || ""); });
    assert.deepStrictEqual(grids.map(function (g) { return g.c.length; }), [3, 1, 2]);
    assert.ok(grids.every(function (g) {
        return g.c.every(function (n) { return /deck-node-button/.test(n.a["class"]); });
    }));
    assert.deepStrictEqual(grids[0].c.map(function (n) { return n.c[0].a["data-action"]; }), ["a.one", "a.two", "a.three"]);
});

test("a group with one column, no title or a nested non-button node draws no grid", function () {
    var v = display.renderTree(groupTree([
        {kind: "group", title: null, items: [btn("b.one"), btn("b.two")]},
        {kind: "group", title: "", columns: 1, items: [{kind: "text", text: "x"}]}
    ]), CONNECTED);
    assert.strictEqual(findV(v, function (n) { return /deck-grid/.test(n.a["class"] || ""); }).length, 0);
    assert.strictEqual(findV(v, function (n) { return n.a["class"] === "deck-group-title"; }).length, 0);
    assert.strictEqual(byAction(v, "b.one").length, 1);
    assert.strictEqual(byAction(v, "b.two").length, 1);
});

test("group buttons keep busy, disabled, confirm and offline behaviour", function () {
    var items = [{kind: "group", title: "G", columns: 2, items: [
        btn("g.busy", {busy: true}), btn("g.off", {disabled: true}), btn("g.ask", {confirm: "Sure?"})
    ]}];
    var v = display.renderTree(groupTree(items), CONNECTED);
    assert.ok(/is-busy/.test(byAction(v, "g.busy")[0].a["class"]));
    assert.strictEqual(byAction(v, "g.off")[0].p.disabled, true);
    assert.strictEqual(findV(v, function (n) { return n.a["class"] === "deck-confirm"; }).length, 0);
    var asking = display.renderTree(groupTree(items), {connection: "connected", collapsed: {}, confirming: "g.ask"});
    assert.strictEqual(findV(asking, function (n) { return n.a["class"] === "deck-confirm"; }).length, 1);
    var cell = findV(asking, function (n) { return /is-confirming/.test(n.a["class"] || ""); });
    assert.strictEqual(cell.length, 1, "the asking button's cell spans the full row");
    var offline = display.renderTree(groupTree(items), {connection: "connecting", collapsed: {}});
    assert.strictEqual(byAction(offline, "g.busy")[0].p.disabled, true);
});

test("group keys: titles identify groups, repeated titles stay distinct", function () {
    var v = display.renderTree(groupTree([
        {kind: "group", title: "Same", items: [btn("k.a")]},
        {kind: "group", title: "Same", items: [btn("k.b")]},
        {kind: "group", title: "Other", items: [btn("k.c")]}
    ]), CONNECTED);
    var body = findV(v, function (n) { return n.a["class"] === "deck-section-body"; })[0];
    assert.deepStrictEqual(body.c.map(function (n) { return n.k; }), ["g:Same", "g:Same#2", "g:Other"]);
});

test("ids inside groups reconcile across re-renders", function () {
    function build(layout) {
        return groupTree(layout);
    }
    var inputNode = {kind: "input", id: "q.search", label: "Search", value: "", placeholder: "Type", submit_label: "Go"};
    var first = display.renderTree(build([
        {kind: "group", title: "Timeline", columns: 2, items: [btn("t.one"), btn("t.two")]},
        {kind: "group", title: "Find", columns: 2, items: [inputNode, btn("q.go"), btn("q.more")]}
    ]), CONNECTED);
    var doc = fakeDocument();
    var container = doc.createElement("div");
    display.applyOps(container, display.diff(null, first));
    var input = find(container, function (n) { return n.attributes["data-action"] === "q.search"; });
    var go = find(container, function (n) { return n.attributes["data-action"] === "q.go"; });
    var two = find(container, function (n) { return n.attributes["data-action"] === "t.two"; });
    assert.ok(input && go && two);
    input.value = "typed";

    // A group is inserted before, a button joins the first group, one button of the second is removed.
    var second = display.renderTree(build([
        {kind: "group", title: "New", columns: 2, items: [btn("n.one")]},
        {kind: "group", title: "Timeline", columns: 2, items: [btn("t.one"), btn("t.two"), btn("t.three")]},
        {kind: "group", title: "Find", columns: 2, items: [inputNode, btn("q.go")]}
    ]), CONNECTED);
    display.applyOps(container, display.diff(first, second));
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "q.search"; }), input);
    assert.strictEqual(input.value, "typed", "the user's text survives");
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "q.go"; }), go);
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "t.two"; }), two);
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "q.more"; }), null);
    assert.ok(find(container, function (n) { return n.attributes["data-action"] === "t.three"; }));
    var titles = [];
    find(container, function (n) {
        if (n.attributes["class"] === "deck-group-title") {
            titles.push(n.textContent);
        }
        return false;
    });
    assert.deepStrictEqual(titles, ["New", "Timeline", "Find"]);

    // The patched DOM equals a fresh render of the same tree.
    function serialise(node) {
        return {t: node.tagName, a: node.attributes, x: node.childNodes.length ? null : node._text, c: node.childNodes.map(serialise)};
    }
    var fresh = doc.createElement("div");
    display.applyOps(fresh, display.diff(null, second));
    assert.deepStrictEqual(serialise(container), serialise(fresh));
});

test("empty list, toast and connection notices", function () {
    var tree = sampleTree();
    tree.sections[0].items = [{kind: "list", id: "l", rows: [], empty: "No results"}];
    var v = display.renderTree(tree, {connection: "connected", collapsed: {}, toast: {text: "Done", tone: "ok"}});
    assert.strictEqual(findV(v, function (n) { return n.x === "No results"; }).length, 1);
    assert.strictEqual(findV(v, function (n) { return n.x === "Done" && /deck-toast tone-ok/.test(n.a["class"]); }).length, 1);
    var restart = display.renderTree(tree, {connection: "restart_pending", collapsed: {}});
    assert.strictEqual(findV(restart, function (n) { return n.x === "Restart Premiere Pro to finish the Premiere Deck update"; }).length, 1);
    assert.strictEqual(display.headerStatus(tree, {connection: "restart_pending"}).text, "Update available · Restart Premiere Pro");
    assert.strictEqual(display.headerStatus(tree, {connection: "protocol", hostVersion: "2.0.0"}).text, "Update Premiere Deck (host 2.0.0)");
    var noHost = display.renderTree(null, {connection: "connecting", noHost: true, hostExe: "C:\\PremiereDeck.exe", collapsed: {}});
    assert.strictEqual(findV(noHost, function (n) { return n.x === "Start Premiere Deck"; }).length, 1);
    var noExe = display.renderTree(null, {connection: "connecting", noHost: true, hostExe: null, collapsed: {}});
    assert.strictEqual(findV(noExe, function (n) { return n.x === "Start PremiereDeck.exe to connect"; }).length, 1);
});

test("the panel is always dark: no theme code follows Premiere's skin", function () {
    assert.strictEqual(display.themeVars, undefined);
    var css = fs.readFileSync(path.join(PANEL, "css", "panel.css"), "utf8");
    assert.ok(/--deck-bg-top:\s*#05030c/i.test(css) && /--deck-bg-mid:\s*#0f0922/i.test(css) && /--deck-bg-bottom:\s*#1c103e/i.test(css));
    assert.ok(!/@import|url\(["']?https?:/.test(css), "no imported or remote styles");
});

test("header and sections render as pills and cards", function () {
    var v = display.renderTree(sampleTree(), CONNECTED);
    var header = findV(v, function (n) { return n.t === "header"; })[0];
    assert.strictEqual(findV(header, function (n) { return /deck-glyph/.test(n.a["class"] || ""); }).length, 1);
    var pill = findV(header, function (n) { return /deck-pill/.test(n.a["class"] || ""); })[0];
    assert.ok(/tone-ok/.test(pill.a["class"]));
    assert.strictEqual(findV(pill, function (n) { return n.a["class"] === "deck-dot"; }).length, 1);
    var head = findV(v, function (n) { return n.a["data-section"] === "files"; })[0];
    assert.deepStrictEqual(head.c.map(function (n) { return n.k; }), ["title", "status", "chevron"]);
    assert.strictEqual(head.a["aria-expanded"], "true");
    var collapsed = findV(v, function (n) { return n.a["data-section"] === "actions"; })[0];
    assert.strictEqual(collapsed.a["aria-expanded"], "false");
    assert.deepStrictEqual(collapsed.c.map(function (n) { return n.k; }), ["title", "chevron"]);
});

test("common.isDevBrowser is false under Node and inside CEP", function () {
    assert.strictEqual(common.isDevBrowser(), false);
});

// --- display.js: diff and applyOps ----------------------------------------------------------------------------

test("diff of identical trees is empty", function () {
    var a = display.renderTree(sampleTree(), CONNECTED);
    var b = display.renderTree(sampleTree(), CONNECTED);
    assert.deepStrictEqual(display.diff(a, b), []);
});

test("diff emits text, attrs and props operations", function () {
    var a = display.renderTree(sampleTree(), CONNECTED);
    var tree = sampleTree();
    tree.sections[0].items[5] = {kind: "text", text: "Changed", tone: "error"};
    tree.sections[0].items[2] = {kind: "toggle", id: "files.watch", label: "Watch", value: false};
    var ops = display.diff(a, display.renderTree(tree, CONNECTED));
    var kinds = ops.map(function (op) { return op.op; });
    assert.ok(kinds.indexOf("text") >= 0);
    assert.ok(kinds.indexOf("attrs") >= 0);
    var props = ops.filter(function (op) { return op.op === "props"; });
    assert.strictEqual(props.length, 1);
    assert.deepStrictEqual(props[0].set, {checked: false});
    assert.strictEqual(ops.filter(function (op) { return op.op === "replace"; }).length, 0);
});

test("a select's value is set after its options change", function () {
    var a = display.renderTree(sampleTree(), CONNECTED);
    var tree = sampleTree();
    tree.sections[0].items[3] = {kind: "select", id: "files.layout", label: "Layout", value: "c",
        options: [{value: "a", label: "A"}, {value: "c", label: "C"}]};
    var ops = display.diff(a, display.renderTree(tree, CONNECTED));
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

test("applyOps keeps elements by id across reorder, insert and removal", function () {
    var doc = fakeDocument();
    var container = doc.createElement("div");
    var first = display.renderTree(sampleTree(), CONNECTED);
    display.applyOps(container, display.diff(null, first));
    var input = find(container, function (n) { return n.attributes["data-action"] === "files.find"; });
    var list = find(container, function (n) { return n.attributes["data-list"] === "files.results"; });
    assert.ok(input && list);
    input.value = "typed by the user";

    var tree = sampleTree();
    var items = tree.sections[0].items;
    var listNode = items.pop();
    listNode.rows.reverse();
    listNode.rows.push({id: "r3", title: "Three", actions: []});
    items.unshift(listNode);
    items.splice(items.indexOf(items.filter(function (n) { return n.id === "files.busy"; })[0]), 1);
    var second = display.renderTree(tree, CONNECTED);
    display.applyOps(container, display.diff(first, second));

    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "files.find"; }), input);
    assert.strictEqual(input.value, "typed by the user", "an unchanged host value does not overwrite the user's text");
    assert.strictEqual(find(container, function (n) { return n.attributes["data-list"] === "files.results"; }), list);
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "files.busy"; }), null);
    var titles = [];
    find(list, function (n) {
        if (n.attributes["class"] === "deck-list-title") {
            titles.push(n.textContent);
        }
        return false;
    });
    assert.deepStrictEqual(titles, ["Two", "One", "Three"]);
    var body = find(container, function (n) { return n.attributes["class"] === "deck-section-body"; });
    assert.strictEqual(body.childNodes[0].attributes["data-list"], "files.results");

    // A changed host value is applied.
    var third = sampleTree();
    third.sections[0].items[4] = {kind: "input", id: "files.find", label: "Find", value: "", placeholder: "Search"};
    var thirdV = display.renderTree(third, CONNECTED);
    display.applyOps(container, display.diff(second, thirdV));
    assert.strictEqual(find(container, function (n) { return n.attributes["data-action"] === "files.find"; }).value, "");
});

test("applyOps result equals a fresh render", function () {
    function serialise(node) {
        return {t: node.tagName, a: node.attributes, x: node.childNodes.length ? null : node._text,
            c: node.childNodes.map(serialise)};
    }
    var doc = fakeDocument();
    var patched = doc.createElement("div");
    var a = display.renderTree(sampleTree(), CONNECTED);
    display.applyOps(patched, display.diff(null, a));
    var tree = sampleTree({title: "Other"});
    tree.sections.reverse();
    tree.sections[0].collapsed = false;
    var b = display.renderTree(tree, {connection: "connected", collapsed: {}, confirming: "files.collect", toast: {text: "t", tone: "ok"}});
    display.applyOps(patched, display.diff(a, b));
    var fresh = doc.createElement("div");
    display.applyOps(fresh, display.diff(null, b));
    assert.deepStrictEqual(serialise(patched), serialise(fresh));
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
