/*
 * Premiere Deck panel: the visible display (docs/PANEL.md 2, docs/PROTOCOL.md 6).
 * Renders the UI tree the host sends and sends the user's actions back. It knows no module and no action id.
 *
 * Rendering is split into pure functions, exported for `node tests/panel/run.js`:
 *   renderTree(tree, local) -> vnode           the whole panel as plain data
 *   diff(oldVnode, newVnode) -> operations      what to change in the DOM (keyed by node id)
 * and one impure applier (applyOps) that touches the DOM. Event handling is delegated through data attributes,
 * so vnodes stay plain data.
 */
(function (root) {
    "use strict";

    var NO_HOST_MS = 10000;
    var RESIZE_DEBOUNCE_MS = 300;
    var COLLAPSE_KEY = "deck.section.";

    // --- vnodes ------------------------------------------------------------------------------------------------

    // h(tag, key, attrs, props, childrenOrText)
    function h(tag, key, attrs, props, content) {
        var node = {t: tag, k: key, a: attrs || {}, p: props || {}, c: [], x: null};
        if (typeof content === "string") {
            node.x = content;
        } else if (content) {
            for (var i = 0; i < content.length; i++) {
                if (content[i]) {
                    node.c.push(content[i]);
                }
            }
        }
        return node;
    }

    function cls() {
        var names = [];
        for (var i = 0; i < arguments.length; i++) {
            if (arguments[i]) {
                names.push(arguments[i]);
            }
        }
        return names.join(" ");
    }

    function tone(value) {
        return "tone-" + (value || "neutral");
    }

    function hintAttrs(node, attrs) {
        if (node.hint) {
            attrs.title = String(node.hint);
        }
        return attrs;
    }

    // Context while rendering one tree: local UI state plus "offline" (controls disabled while the host is away).
    function buttonNode(node, ctx, rowId, key) {
        var disabled = !!node.disabled || ctx.offline;
        var confirmKey = node.id + (rowId ? "|" + rowId : "");
        var attrs = hintAttrs(node, {
            "class": cls("deck-btn", tone(node.tone), node.busy && "is-busy", disabled && "is-disabled"),
            "data-action": node.id,
            "data-kind": "button"
        });
        if (rowId) {
            attrs["data-row"] = rowId;
        }
        if (node.confirm) {
            attrs["data-confirm"] = confirmKey;
        }
        var button = h("button", "btn", attrs, {disabled: disabled}, [
            node.busy ? h("span", "spin", {"class": "deck-spinner"}, null, "") : null,
            h("span", "label", {"class": "deck-btn-label"}, null, String(node.label || ""))
        ]);
        var children = [button];
        var confirming = !!node.confirm && ctx.local.confirming === confirmKey && !disabled && !node.busy;
        if (confirming) {
            var yesAttrs = {"class": "deck-btn tone-accent is-small deck-confirm-yes", "data-confirm-yes": confirmKey, "data-action": node.id};
            if (rowId) {
                yesAttrs["data-row"] = rowId;
            }
            children.push(h("div", "confirm", {"class": "deck-confirm"}, null, [
                h("span", "q", {"class": "deck-confirm-text"}, null, String(node.confirm)),
                h("button", "yes", yesAttrs, {}, "Yes"),
                h("button", "no", {"class": "deck-btn is-small deck-confirm-no", "data-confirm-no": confirmKey}, {}, "No")
            ]));
        }
        return h("div", key, {"class": cls("deck-node deck-node-button", confirming && "is-confirming")}, null, children);
    }

    function toggleNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        return h("label", key, hintAttrs(node, {"class": cls("deck-node deck-toggle", disabled && "is-disabled")}), null, [
            h("input", "input", {type: "checkbox", "class": "deck-toggle-input", "data-action": node.id, "data-kind": "toggle"},
                {checked: !!node.value, disabled: disabled}),
            h("span", "track", {"class": "deck-toggle-track"}, null, ""),
            h("span", "label", {"class": "deck-toggle-label"}, null, String(node.label || ""))
        ]);
    }

    function selectNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        var options = [];
        var list = node.options || [];
        for (var i = 0; i < list.length; i++) {
            var value = String(list[i].value);
            options.push(h("option", "o:" + value, {value: value}, {}, String(list[i].label)));
        }
        return h("label", key, hintAttrs(node, {"class": cls("deck-node deck-field", disabled && "is-disabled")}), null, [
            h("span", "label", {"class": "deck-field-label"}, null, String(node.label || "")),
            h("select", "select", {"class": "deck-select", "data-action": node.id, "data-kind": "select"},
                {value: node.value === undefined || node.value === null ? "" : String(node.value), disabled: disabled}, options)
        ]);
    }

    function inputNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        var row = [
            h("input", "input", {type: "text", "class": "deck-input", "data-action": node.id, "data-kind": "input",
                placeholder: String(node.placeholder || "")},
                {value: node.value === undefined || node.value === null ? "" : String(node.value), disabled: disabled})
        ];
        if (node.submit_label) {
            row.push(h("button", "submit", {"class": "deck-btn tone-accent is-small", "data-submit": node.id}, {disabled: disabled},
                String(node.submit_label)));
        }
        return h("div", key, hintAttrs(node, {"class": cls("deck-node deck-field", disabled && "is-disabled")}), null, [
            node.label ? h("span", "label", {"class": "deck-field-label"}, null, String(node.label)) : null,
            h("div", "row", {"class": "deck-input-row"}, null, row)
        ]);
    }

    function textNode(node, key) {
        return h("div", key, {"class": cls("deck-node deck-text", tone(node.tone), node.small && "is-small")}, null,
            String(node.text === undefined ? "" : node.text));
    }

    function progressNode(node, key) {
        var indeterminate = node.value === null || node.value === undefined;
        var pct = indeterminate ? 100 : Math.max(0, Math.min(1, Number(node.value))) * 100;
        return h("div", key, {"class": cls("deck-node deck-progress", indeterminate && "is-indeterminate")}, null, [
            node.label ? h("div", "label", {"class": "deck-progress-label"}, null, String(node.label)) : null,
            h("div", "track", {"class": "deck-progress-track"}, null, [
                h("div", "fill", {"class": "deck-progress-fill", style: "width: " + pct.toFixed(1) + "%"}, null, "")
            ])
        ]);
    }

    // The thin strength bar under a list row's subtitle (PROTOCOL 6.1 `list` row `bar`, 0..1).
    function barNode(value) {
        var pct = Math.max(0, Math.min(1, value)) * 100;
        return h("div", "bar", {"class": "deck-list-bar"}, null, [
            h("div", "fill", {"class": "deck-list-bar-fill", style: "width: " + pct.toFixed(1) + "%"}, null, "")
        ]);
    }

    function listNode(node, ctx, key) {
        var rows = node.rows || [];
        var children = [];
        if (!rows.length) {
            children.push(h("div", "empty", {"class": "deck-list-empty"}, null, String(node.empty || "")));
        }
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var actions = [];
            var buttons = row.actions || [];
            for (var j = 0; j < buttons.length; j++) {
                actions.push(buttonNode(buttons[j], ctx, row.id, "a:" + buttons[j].id));
            }
            // A row whose button asks for confirmation wraps, so the question gets the full row width.
            var asking = !!ctx.local.confirming && String(ctx.local.confirming).split("|")[1] === String(row.id);
            children.push(h("div", "r:" + row.id, {"class": cls("deck-list-row", tone(row.tone), asking && "is-confirming")}, null, [
                row.thumb ? h("img", "thumb", {"class": "deck-list-thumb", src: String(row.thumb), alt: ""}, null, "") : null,
                h("div", "text", {"class": "deck-list-text"}, null, [
                    h("div", "title", {"class": "deck-list-title"}, null, String(row.title || "")),
                    row.subtitle ? h("div", "sub", {"class": "deck-list-subtitle"}, null, String(row.subtitle)) : null,
                    typeof row.bar === "number" ? barNode(row.bar) : null
                ]),
                actions.length ? h("div", "actions", {"class": "deck-list-actions"}, null, actions) : null
            ]));
        }
        return h("div", key, {"class": "deck-node deck-list", "data-list": node.id}, null, children);
    }

    // A `group` (PROTOCOL 6.1): a caption and its items. With columns 2, every run of consecutive buttons becomes a
    // two-column grid; any other node takes the full width and ends the run. Items keep their id keys inside the
    // group, so a focused input or a scrolled list survives a re-render that changes the neighbours.
    function groupNode(node, ctx, key) {
        var columns = Number(node.columns) === 2 ? 2 : 1;
        var items = renderNodes(node.items, ctx);
        var body = [];
        var run = [];
        var flush = function () {
            if (run.length) {
                body.push(h("div", "grid:" + run[0].k, {"class": "deck-grid"}, null, run));
                run = [];
            }
        };
        for (var i = 0; i < items.length; i++) {
            if (columns === 2 && items[i].a["class"].indexOf("deck-node-button") >= 0) {
                run.push(items[i]);
            } else {
                flush();
                body.push(items[i]);
            }
        }
        flush();
        var title = node.title === undefined || node.title === null ? "" : String(node.title);
        return h("div", key, {"class": cls("deck-node deck-group", columns === 2 && "has-columns")}, null, [
            title ? h("div", "caption", {"class": "deck-group-title"}, null, title) : null,
            h("div", "items", {"class": "deck-group-items"}, null, body)
        ]);
    }

    function renderNodes(nodes, ctx) {
        var out = [];
        var groupSeen = {};
        for (var i = 0; i < (nodes || []).length; i++) {
            var node = nodes[i];
            if (!node || typeof node !== "object") {
                continue;
            }
            // Interactive nodes are keyed by id (they keep focus and scroll); a group by its title; the rest by
            // position and kind.
            var key = node.id ? "n:" + node.id : "p:" + i + ":" + node.kind;
            if (node.kind === "group") {
                var groupId = "g:" + (node.title === undefined || node.title === null ? "" : String(node.title));
                groupSeen[groupId] = (groupSeen[groupId] || 0) + 1;
                key = groupSeen[groupId] > 1 ? groupId + "#" + groupSeen[groupId] : groupId;
            }
            var vnode = null;
            switch (node.kind) {
            case "button":
                vnode = buttonNode(node, ctx, null, key);
                break;
            case "toggle":
                vnode = toggleNode(node, ctx, key);
                break;
            case "select":
                vnode = selectNode(node, ctx, key);
                break;
            case "input":
                vnode = inputNode(node, ctx, key);
                break;
            case "text":
                vnode = textNode(node, key);
                break;
            case "progress":
                vnode = progressNode(node, key);
                break;
            case "list":
                vnode = listNode(node, ctx, key);
                break;
            case "row":
                vnode = h("div", key, {"class": "deck-node deck-row"}, null, renderNodes(node.items, ctx));
                break;
            case "group":
                vnode = groupNode(node, ctx, key);
                break;
            case "divider":
                vnode = h("hr", key, {"class": "deck-divider"}, null, "");
                break;
            default:
                vnode = null;
            }
            if (vnode) {
                out.push(vnode);
            }
        }
        return out;
    }

    // A status pill: a dot and a line of text in the tone's colour.
    function pill(key, extra, status) {
        return h("span", key, {"class": cls("deck-pill", extra, tone(status.tone))}, null, [
            h("span", "dot", {"class": "deck-dot"}, null, ""),
            h("span", "text", {"class": "deck-pill-text"}, null, String(status.text || ""))
        ]);
    }

    function isCollapsed(section, local) {
        if (local.collapsed && local.collapsed.hasOwnProperty(section.id)) {
            return !!local.collapsed[section.id];
        }
        return !!section.collapsed;
    }

    function headerStatus(tree, local) {
        if (local.connection === "restart_pending") {
            return {text: "Update available · Restart Premiere Pro", tone: "warn"};
        }
        if (local.connection === "protocol") {
            return {text: "Update Premiere Deck" + (local.hostVersion ? " (host " + local.hostVersion + ")" : ""), tone: "error"};
        }
        if (local.connection !== "connected" && local.connection !== "restart_pending") {
            return {text: "Connecting to Premiere Deck…", tone: "neutral"};
        }
        if (tree && tree.status) {
            return tree.status;
        }
        return {text: "Connected", tone: "ok"};
    }

    /*
     * The whole panel as a vnode. `local` is the display's own state:
     *   connection   "connecting" | "connected" | "restart_pending" | "protocol"
     *   noHost       true after 10 s without a host
     *   hostExe      host_exe from deck.json or null
     *   startStatus  result line of the last "Start Premiere Deck" click or null
     *   collapsed    {sectionId: bool} remembered by the user
     *   confirming   key of the button whose confirmation is open, or null
     *   toast        {text, tone} or null
     *   hostVersion  host version for the protocol notice
     */
    function renderTree(tree, local) {
        local = local || {};
        var connected = local.connection === "connected" || local.connection === "restart_pending";
        var ctx = {local: local, offline: !connected};
        var status = headerStatus(tree, local);
        var children = [];

        children.push(h("header", "header", {"class": "deck-header"}, null, [
            h("span", "glyph", {"class": "deck-glyph"}, null, ""),
            h("div", "title", {"class": "deck-title"}, null, String((tree && tree.title) || "Premiere Deck")),
            pill("status", "deck-status", status)
        ]));

        if (local.connection === "restart_pending") {
            children.push(h("div", "notice", {"class": "deck-notice tone-warn"}, null,
                "Restart Premiere Pro to finish the Premiere Deck update"));
        }

        if (!connected && local.noHost) {
            var offline = [];
            if (local.hostExe) {
                offline.push(h("button", "start", {"class": "deck-btn tone-accent", "data-start-host": "1"}, {}, "Start Premiere Deck"));
            } else {
                offline.push(h("div", "text", {"class": "deck-text"}, null, "Start PremiereDeck.exe to connect"));
            }
            if (local.startStatus) {
                offline.push(h("div", "result", {"class": "deck-text is-small"}, null, String(local.startStatus)));
            }
            children.push(h("div", "offline", {"class": "deck-offline"}, null, offline));
        }

        var sections = [];
        var list = (tree && tree.sections) || [];
        for (var i = 0; i < list.length; i++) {
            var section = list[i];
            var collapsed = isCollapsed(section, local);
            sections.push(h("section", "s:" + section.id, {"class": cls("deck-section", collapsed && "is-collapsed")}, null, [
                h("div", "head", {"class": "deck-section-head", "data-section": section.id, role: "button", tabindex: "0",
                    "aria-expanded": collapsed ? "false" : "true"}, null, [
                    h("span", "title", {"class": "deck-section-title"}, null, String(section.title || "")),
                    section.status ? pill("status", "deck-section-status", section.status) : null,
                    h("span", "chevron", {"class": "deck-chevron"}, null, "")
                ]),
                collapsed ? null : h("div", "body", {"class": "deck-section-body"}, null, renderNodes(section.items, ctx))
            ]));
        }
        children.push(h("main", "body", {"class": cls("deck-body", !connected && "is-offline")}, null, sections));

        var footer = tree && tree.footer;
        if (footer) {
            var parts = [h("span", "text", {"class": "deck-footer-text"}, null, String(footer.text || ""))];
            if (footer.link && footer.link.action) {
                parts.push(h("button", "link", {"class": "deck-link", "data-action": footer.link.action, "data-kind": "link"},
                    {disabled: !connected}, String(footer.link.label || "")));
            }
            children.push(h("footer", "footer", {"class": "deck-footer"}, null, parts));
        }

        if (local.toast) {
            children.push(h("div", "toast", {"class": cls("deck-toast", tone(local.toast.tone))}, null, String(local.toast.text || "")));
        }
        return h("div", "root", {"class": "deck"}, null, children);
    }

    // --- diff --------------------------------------------------------------------------------------------------

    // Effective key of a child: tag and key, so a key reused by another tag is a new element.
    function childKey(vnode) {
        return vnode.t + "|" + vnode.k;
    }

    /*
     * Operations that turn the DOM of `oldV` into `newV`. Paths are lists of child keys from the root element.
     *   {op: "replace", path, vnode}                 replace the element (root missing or tag changed)
     *   {op: "text", path, text}                     textContent of a leaf
     *   {op: "attrs", path, set: {}, remove: []}     attributes
     *   {op: "props", path, set: {}}                 DOM properties (value, checked, disabled)
     *   {op: "children", path, order: [keys], create: {key: vnode}}  child order; keys not in `create` are kept
     * Child order and attributes of a parent come before its children's operations; its properties come after them.
     */
    function diff(oldV, newV, path, ops) {
        path = path || [];
        ops = ops || [];
        if (!oldV || oldV.t !== newV.t) {
            ops.push({op: "replace", path: path, vnode: newV});
            return ops;
        }
        var set = {};
        var remove = [];
        var changed = false;
        var name;
        for (name in newV.a) {
            if (newV.a.hasOwnProperty(name) && oldV.a[name] !== newV.a[name]) {
                set[name] = newV.a[name];
                changed = true;
            }
        }
        for (name in oldV.a) {
            if (oldV.a.hasOwnProperty(name) && !newV.a.hasOwnProperty(name)) {
                remove.push(name);
                changed = true;
            }
        }
        if (changed) {
            ops.push({op: "attrs", path: path, set: set, remove: remove});
        }
        var props = {};
        var propsChanged = false;
        for (name in newV.p) {
            if (newV.p.hasOwnProperty(name) && oldV.p[name] !== newV.p[name]) {
                props[name] = newV.p[name];
                propsChanged = true;
            }
        }
        var propsOp = propsChanged ? {op: "props", path: path, set: props} : null;
        if (newV.x !== null || oldV.x !== null) {
            if (newV.x !== oldV.x) {
                if (newV.x !== null && oldV.c.length === 0 && newV.c.length === 0) {
                    ops.push({op: "text", path: path, text: newV.x});
                    if (propsOp) {
                        ops.push(propsOp);
                    }
                    return ops;
                }
                ops.push({op: "replace", path: path, vnode: newV});
                return ops;
            }
            if (newV.x !== null) {
                if (propsOp) {
                    ops.push(propsOp);
                }
                return ops;
            }
        }
        var oldByKey = {};
        var oldKeys = [];
        var i;
        for (i = 0; i < oldV.c.length; i++) {
            oldByKey[childKey(oldV.c[i])] = oldV.c[i];
            oldKeys.push(childKey(oldV.c[i]));
        }
        var newKeys = [];
        var create = {};
        var createCount = 0;
        for (i = 0; i < newV.c.length; i++) {
            var key = childKey(newV.c[i]);
            newKeys.push(key);
            if (!oldByKey.hasOwnProperty(key)) {
                create[key] = newV.c[i];
                createCount++;
            }
        }
        if (createCount || oldKeys.join("\n") !== newKeys.join("\n")) {
            ops.push({op: "children", path: path, order: newKeys, create: create});
        }
        for (i = 0; i < newV.c.length; i++) {
            var childK = newKeys[i];
            if (oldByKey.hasOwnProperty(childK)) {
                diff(oldByKey[childK], newV.c[i], path.concat([childK]), ops);
            }
        }
        // Properties last: a select's value needs its options in place.
        if (propsOp) {
            ops.push(propsOp);
        }
        return ops;
    }

    // --- DOM ---------------------------------------------------------------------------------------------------

    var PROPS_LAST = {value: true};

    function setProps(el, props) {
        for (var name in props) {
            if (props.hasOwnProperty(name) && !PROPS_LAST[name]) {
                el[name] = props[name];
            }
        }
        // A select's value is applied after its options exist.
        if (props.hasOwnProperty("value") && el.value !== props.value) {
            el.value = props.value;
        }
    }

    function createElement(doc, vnode) {
        var el = doc.createElement(vnode.t);
        el.__deckKey = childKey(vnode);
        for (var name in vnode.a) {
            if (vnode.a.hasOwnProperty(name)) {
                el.setAttribute(name, vnode.a[name]);
            }
        }
        if (vnode.x !== null) {
            el.textContent = vnode.x;
        } else {
            for (var i = 0; i < vnode.c.length; i++) {
                el.appendChild(createElement(doc, vnode.c[i]));
            }
        }
        setProps(el, vnode.p);
        return el;
    }

    function resolve(container, path) {
        var el = container.firstChild;
        for (var i = 0; i < path.length && el; i++) {
            var next = null;
            for (var child = el.firstChild; child; child = child.nextSibling) {
                if (child.__deckKey === path[i]) {
                    next = child;
                    break;
                }
            }
            el = next;
        }
        return el;
    }

    function applyOps(container, ops) {
        var doc = container.ownerDocument;
        for (var i = 0; i < ops.length; i++) {
            var op = ops[i];
            if (op.op === "replace" && op.path.length === 0) {
                while (container.firstChild) {
                    container.removeChild(container.firstChild);
                }
                container.appendChild(createElement(doc, op.vnode));
                continue;
            }
            var el = resolve(container, op.path);
            if (!el) {
                continue;
            }
            if (op.op === "replace") {
                el.parentNode.replaceChild(createElement(doc, op.vnode), el);
            } else if (op.op === "text") {
                el.textContent = op.text;
            } else if (op.op === "attrs") {
                for (var name in op.set) {
                    if (op.set.hasOwnProperty(name)) {
                        el.setAttribute(name, op.set[name]);
                    }
                }
                for (var r = 0; r < op.remove.length; r++) {
                    el.removeAttribute(op.remove[r]);
                }
            } else if (op.op === "props") {
                setProps(el, op.set);
            } else if (op.op === "children") {
                var existing = {};
                for (var child = el.firstChild; child; child = child.nextSibling) {
                    if (child.__deckKey) {
                        existing[child.__deckKey] = child;
                    }
                }
                var wanted = {};
                for (var k = 0; k < op.order.length; k++) {
                    var key = op.order[k];
                    wanted[key] = true;
                    var node = op.create.hasOwnProperty(key) ? createElement(doc, op.create[key]) : existing[key];
                    if (!node) {
                        continue;
                    }
                    var at = el.childNodes[k] || null;
                    if (at !== node) {
                        el.insertBefore(node, at);
                    }
                }
                for (var key2 in existing) {
                    if (existing.hasOwnProperty(key2) && (!wanted[key2] || op.create.hasOwnProperty(key2)) && existing[key2].parentNode === el) {
                        el.removeChild(existing[key2]);
                    }
                }
            }
        }
    }

    // --- runtime (CEP or a browser) ----------------------------------------------------------------------------

    function Display(container) {
        this.container = container;
        this.common = root.DeckCommon;
        this.tree = null;
        this.vnode = null;
        this.local = {
            connection: "connecting",
            noHost: false,
            hostExe: null,
            startStatus: null,
            collapsed: loadCollapsed(),
            confirming: null,
            toast: null,
            hostVersion: ""
        };
        this.toastTimer = null;
        this.noHostTimer = null;
        this.resizeTimer = null;
        this.client = null;
    }

    function loadCollapsed() {
        var result = {};
        try {
            for (var i = 0; i < root.localStorage.length; i++) {
                var key = root.localStorage.key(i);
                if (key && key.indexOf(COLLAPSE_KEY) === 0) {
                    result[key.substring(COLLAPSE_KEY.length)] = root.localStorage.getItem(key) === "1";
                }
            }
        } catch (e) {
            // No storage: sections start as the host says.
        }
        return result;
    }

    Display.prototype.render = function () {
        var next = renderTree(this.tree, this.local);
        var ops = diff(this.vnode, next);
        applyOps(this.container, ops);
        this.vnode = next;
    };

    Display.prototype.start = function () {
        var self = this;
        this.client = new this.common.DeckClient({
            role: "display",
            onMessage: function (message) {
                self.onMessage(message);
            },
            onState: function (state, info) {
                self.onState(state, info);
            },
            onWelcome: function () {
                self.sendReady();
            }
        });
        this.bindEvents();
        this.armNoHost();
        this.render();
        this.client.start();
    };

    Display.prototype.armNoHost = function () {
        var self = this;
        if (this.noHostTimer) {
            return;
        }
        this.noHostTimer = setTimeout(function () {
            self.noHostTimer = null;
            if (self.local.connection !== "connected" && self.local.connection !== "restart_pending") {
                var deck = self.common.readDeck();
                self.local.noHost = true;
                self.local.hostExe = deck ? deck.host_exe : null;
                self.render();
            }
        }, NO_HOST_MS);
    };

    Display.prototype.onState = function (state, info) {
        this.local.connection = state;
        this.local.hostVersion = info && info.host_version ? info.host_version : this.local.hostVersion;
        if (state === "connected" || state === "restart_pending") {
            this.local.noHost = false;
            this.local.startStatus = null;
            if (this.noHostTimer) {
                clearTimeout(this.noHostTimer);
                this.noHostTimer = null;
            }
        } else {
            this.local.confirming = null;
            this.armNoHost();
        }
        this.render();
    };

    Display.prototype.onMessage = function (message) {
        if (message.type === "ui") {
            this.tree = message;
            if (this.local.confirming && !this.confirmStillValid()) {
                this.local.confirming = null;
            }
            this.render();
        } else if (message.type === "toast") {
            this.showToast(message);
        }
    };

    // An open confirmation survives a re-render only while its button still exists and still asks.
    Display.prototype.confirmStillValid = function () {
        var key = this.local.confirming;
        var found = false;
        var visit = function (nodes, rowId) {
            for (var i = 0; i < (nodes || []).length && !found; i++) {
                var node = nodes[i];
                if (node.kind === "button" && node.confirm && node.id + (rowId ? "|" + rowId : "") === key && !node.busy && !node.disabled) {
                    found = true;
                } else if (node.kind === "row" || node.kind === "group") {
                    visit(node.items, null);
                } else if (node.kind === "list") {
                    for (var r = 0; r < (node.rows || []).length; r++) {
                        visit(node.rows[r].actions, node.rows[r].id);
                    }
                }
            }
        };
        var sections = (this.tree && this.tree.sections) || [];
        for (var s = 0; s < sections.length; s++) {
            visit(sections[s].items, null);
        }
        return found;
    };

    Display.prototype.showToast = function (message) {
        var self = this;
        if (this.toastTimer) {
            clearTimeout(this.toastTimer);
        }
        this.local.toast = {text: message.text, tone: message.tone};
        this.render();
        this.toastTimer = setTimeout(function () {
            self.toastTimer = null;
            self.local.toast = null;
            self.render();
        }, Number(message.ms) > 0 ? Number(message.ms) : 3500);
    };

    Display.prototype.sendAction = function (id, value, row) {
        var message = {type: "action", id: id, revision: this.tree ? this.tree.revision : null};
        if (value !== undefined) {
            message.value = value;
        }
        if (row) {
            message.row = row;
        }
        this.client.send(message);
    };

    Display.prototype.sendReady = function () {
        this.client.send({type: "display.ready", size: {w: root.innerWidth, h: root.innerHeight}});
    };

    function closest(el, attr, stop) {
        while (el && el !== stop && el.nodeType === 1) {
            if (el.hasAttribute(attr)) {
                return el;
            }
            el = el.parentNode;
        }
        return null;
    }

    Display.prototype.bindEvents = function () {
        var self = this;
        var box = this.container;
        var toggleSection = function (el) {
            var id = el.getAttribute("data-section");
            var section = self.findSection(id);
            var now = !isCollapsed(section || {id: id}, self.local);
            self.local.collapsed[id] = now;
            try {
                root.localStorage.setItem(COLLAPSE_KEY + id, now ? "1" : "0");
            } catch (e) {
                // Not remembered without storage.
            }
            self.render();
        };
        box.addEventListener("click", function (event) {
            var target = event.target;
            var el;
            if ((el = closest(target, "data-section", box))) {
                toggleSection(el);
                return;
            }
            if ((el = closest(target, "data-start-host", box))) {
                self.startHost();
                return;
            }
            if ((el = closest(target, "data-confirm-yes", box))) {
                self.local.confirming = null;
                self.render();
                self.sendAction(el.getAttribute("data-action"), undefined, el.getAttribute("data-row") || undefined);
                return;
            }
            if ((el = closest(target, "data-confirm-no", box))) {
                self.local.confirming = null;
                self.render();
                return;
            }
            if ((el = closest(target, "data-submit", box))) {
                var field = el.parentNode.querySelector("input[data-kind=input]");
                if (field) {
                    self.sendAction(el.getAttribute("data-submit"), field.value);
                }
                return;
            }
            el = closest(target, "data-action", box);
            if (!el) {
                return;
            }
            var kind = el.getAttribute("data-kind");
            if (kind !== "button" && kind !== "link") {
                return;
            }
            if (el.disabled || el.className.indexOf("is-busy") >= 0 || el.className.indexOf("is-disabled") >= 0) {
                return;
            }
            if (el.hasAttribute("data-confirm")) {
                var key = el.getAttribute("data-confirm");
                self.local.confirming = self.local.confirming === key ? null : key;
                self.render();
                return;
            }
            self.sendAction(el.getAttribute("data-action"), undefined, el.getAttribute("data-row") || undefined);
        });
        box.addEventListener("change", function (event) {
            var el = event.target;
            var kind = el.getAttribute && el.getAttribute("data-kind");
            if (kind === "toggle") {
                self.sendAction(el.getAttribute("data-action"), !!el.checked);
            } else if (kind === "select") {
                self.sendAction(el.getAttribute("data-action"), el.value);
            }
        });
        box.addEventListener("keydown", function (event) {
            var el = event.target;
            if (event.key === "Enter" && el.getAttribute && el.getAttribute("data-kind") === "input") {
                event.preventDefault();
                self.sendAction(el.getAttribute("data-action"), el.value);
            } else if ((event.key === "Enter" || event.key === " ") && el.getAttribute && el.hasAttribute("data-section")) {
                event.preventDefault();
                toggleSection(el);
            } else if (event.key === "Escape" && self.local.confirming) {
                self.local.confirming = null;
                self.render();
            }
        });
        root.addEventListener("resize", function () {
            if (self.resizeTimer) {
                clearTimeout(self.resizeTimer);
            }
            self.resizeTimer = setTimeout(function () {
                self.resizeTimer = null;
                self.sendReady();
            }, RESIZE_DEBOUNCE_MS);
        });
    };

    Display.prototype.findSection = function (id) {
        var sections = (this.tree && this.tree.sections) || [];
        for (var i = 0; i < sections.length; i++) {
            if (sections[i].id === id) {
                return sections[i];
            }
        }
        return null;
    };

    Display.prototype.startHost = function () {
        var deck = this.common.readDeck();
        var status = this.common.startHost(deck, []);
        var lines = {
            started: "Starting Premiere Deck…",
            missing: "PremiereDeck.exe was not found at " + (deck ? deck.host_exe : ""),
            no_exe: "Start PremiereDeck.exe to connect",
            unsupported: "This Premiere version cannot start programs from a panel"
        };
        this.local.startStatus = lines[status] || "Premiere Deck did not start (" + status + ")";
        this.render();
        if (status === "started") {
            this.client.retryNow();
        }
    };

    var api = {
        h: h,
        renderTree: renderTree,
        renderNodes: renderNodes,
        diff: diff,
        applyOps: applyOps,
        isCollapsed: isCollapsed,
        headerStatus: headerStatus,
        Display: Display
    };

    root.DeckDisplay = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    } else if (root.document) {
        var start = function () {
            root.deckDisplay = new Display(root.document.getElementById("deck"));
            root.deckDisplay.start();
        };
        if (root.document.readyState === "loading") {
            root.document.addEventListener("DOMContentLoaded", start);
        } else {
            start();
        }
    }
}(typeof window !== "undefined" ? window : this));
