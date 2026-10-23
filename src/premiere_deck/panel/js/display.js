/*
 * Premiere Deck panel: the visible display (docs/PANEL.md 2, docs/PROTOCOL.md 6).
 * Renders the UI tree the host sends and sends the user's actions back. It knows no module and no action id.
 *
 * Rendering is split into pure functions, exported for `node tests/panel/run.js`:
 *   renderTree(tree, local) -> vnode           the whole panel as plain data
 *   diff(oldVnode, newVnode) -> operations      what to change in the DOM (keyed by node id)
 *   placeFloating(...), planThemeSwitch(...)    geometry of the tooltip and the menu, the theme switch mode
 * and one impure applier (applyOps) that touches the DOM. Event handling is delegated through data attributes,
 * so vnodes stay plain data. The sprite icons are drawn as <svg><use href="#i-<name>"/></svg>; the sprite
 * (icons.svg) is inlined into the page at start.
 */
(function (root) {
    "use strict";

    var SVG_NS = "http://www.w3.org/2000/svg";
    var SVG_TAGS = {svg: true, use: true};
    var LATE_MS = 15000;          // the soft second line of the connecting screen
    var RESULT_MS = 2000;         // an action result stays on its tile this long
    var LINE_MS = 3000;           // a success or neutral result line hides itself after this long
    var LINE_ERROR_MS = 10000;    // an error or warn result line
    var TILE_COLUMNS = 4;         // tile grids: four columns, three below 360 px (panel.css)
    var TIP_DELAY_MS = 400;
    var FADE_MS = 300;            // the CSS crossfade of a theme switch where View Transitions are missing
    var RESIZE_DEBOUNCE_MS = 300;
    var KEY_GROUP = "deck.group.";
    var KEY_TAB = "deck.tab";
    var KEY_THEME = "deck.theme";
    var EDGE = 12;                // the panel's side gutter: floating cards stay inside it
    var GAP = 8;
    var DEFAULT_THEME = "black";
    var THEMES = {black: true, sky: true};
    var FLASH_CLASS = {ok: "is-done", error: "is-failed", warn: "is-warned", neutral: "is-noted"};
    var OUTCOME_ICON = {ok: "check", error: "cross", warn: "warn", neutral: "info"};
    var PATH_TEXT = /^([A-Za-z]:[\\\/]|\\\\)/;
    var SWATCH = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;   // a menu option's `swatch` colour (PROTOCOL 6.1)

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

    // A sprite icon (docs/PANEL.md 1): the symbol `i-<name>` of icons.svg.
    function icon(name, extra, key) {
        return h("svg", key || "ic", {"class": cls("ic", extra), "aria-hidden": "true"}, null, [
            h("use", "u", {href: "#i-" + name}, null, "")
        ]);
    }

    function glyph(size, extra) {
        return h("svg", "glyph", {"class": cls("deck-glyph", extra), width: String(size), height: String(size),
            viewBox: "0 0 64 64", "aria-hidden": "true"}, null, [h("use", "u", {href: "#glyph-pr"}, null, "")]);
    }

    function pad2(n) {
        return n < 10 ? "0" + n : String(n);
    }

    // "HH:MM" of an epoch-ms time in the panel's local time zone.
    function clock(at) {
        var date = new Date(Number(at) || 0);
        return pad2(date.getHours()) + ":" + pad2(date.getMinutes());
    }

    // The tooltip of a node (docs/PANEL.md 2): `data-tip*` attributes, never a `title` attribute. `force` draws a
    // card from the label alone (icon buttons, tiles whose short label differs from the full one).
    function tipAttrs(node, attrs, key, force) {
        var hint = node.hint ? String(node.hint) : "";
        if (!hint && !force) {
            return attrs;
        }
        attrs["data-tip"] = key;
        attrs["data-tip-label"] = node.label ? String(node.label) : "";
        attrs["data-tip-hint"] = hint;
        if (node.meta) {
            attrs["data-tip-meta"] = String(node.meta);
        }
        return attrs;
    }

    function confirmNode(node, confirmKey, rowId, key) {
        var yesAttrs = {"class": "deck-btn tone-accent is-small deck-confirm-yes", "data-confirm-yes": confirmKey,
            "data-action": node.id};
        if (rowId) {
            yesAttrs["data-row"] = rowId;
        }
        return h("div", key, {"class": "deck-confirm"}, null, [
            h("span", "q", {"class": "deck-confirm-text"}, null, String(node.confirm)),
            h("button", "yes", yesAttrs, {}, "Yes"),
            h("button", "no", {"class": "deck-btn is-small deck-confirm-no", "data-confirm-no": confirmKey}, {}, "No")
        ]);
    }

    /*
     * A `button` node as a list of vnodes: the button itself and, while its inline question is open, the
     * confirmation as a sibling (in a grid it spans the full row under the asking button's row; in a list row it
     * stays inside the row). Forms: an icon tile inside a `tiles` group, a round icon button (`variant` "icon"),
     * a plain pill. The vnode of a button carries `btn: true` (grid runs) and the confirmation `ask: true`.
     */
    function buttonNodes(node, ctx, rowId, key, tiles) {
        var disabled = !!node.disabled || ctx.offline;
        var confirmKey = node.id + (rowId ? "|" + rowId : "");
        var flash = !rowId && ctx.local.flash ? ctx.local.flash[node.id] : null;
        var isIcon = node.variant === "icon";
        var isTile = !!tiles && !isIcon;
        var label = String(node.label || "");
        var attrs = {"data-action": node.id, "data-kind": "button"};
        var vnode;
        if (rowId) {
            attrs["data-row"] = rowId;
        }
        if (node.confirm) {
            attrs["data-confirm"] = confirmKey;
        }
        if (isTile) {
            // 2.1 compact tile: the icon (the result slot) at the left, the code label in one line; `meta` and the
            // menu's badge (the current choice) go into the tooltip, the menu opens from a chevron at the right edge.
            var short = node.short ? String(node.short) : label;
            var badge = node.menu && node.menu.badge !== undefined && node.menu.badge !== null ? String(node.menu.badge) : "";
            attrs["class"] = cls("deck-node deck-tile", tone(node.tone), node.menu && "has-menu", node.busy && "is-busy",
                disabled && "is-disabled", flash && FLASH_CLASS[flash]);
            attrs["aria-label"] = label;
            tipAttrs(node, attrs, key, short !== label || !!node.meta || !!badge);
            var metaLine = [node.meta ? String(node.meta) : "", badge].filter(Boolean).join(" · ");
            if (metaLine) {
                attrs["data-tip-meta"] = metaLine;
            }
            vnode = h("button", key, attrs, {disabled: disabled}, [
                h("span", "face", {"class": "deck-face"}, null, [
                    node.icon ? icon(node.icon, "main", "main") : null,
                    icon("check", "ok", "ok"),
                    icon("cross", "no", "no"),
                    icon("warn", "warn", "warn")
                ]),
                h("span", "text", {"class": "deck-tile-text"}, null, [
                    h("span", "label", {"class": "deck-tile-label"}, null, short)
                ]),
                node.menu ? h("span", "badge", {"class": "deck-badge", "data-menu-open": node.id, "aria-label": badge}, null,
                    [icon("chevron", "", "icon")]) : null
            ]);
        } else if (isIcon) {
            attrs["class"] = cls("deck-node deck-icon-btn", tone(node.tone), node.busy && "is-busy", disabled && "is-disabled");
            attrs["aria-label"] = label;
            tipAttrs(node, attrs, key, true);
            vnode = h("button", key, attrs, {disabled: disabled}, [
                node.busy ? h("span", "spin", {"class": "deck-spinner"}, null, "") : icon(node.icon || "info", "", "icon")
            ]);
        } else {
            attrs["class"] = cls("deck-node deck-btn", tone(node.tone), node.busy && "is-busy", disabled && "is-disabled",
                node.icon && "has-icon");
            tipAttrs(node, attrs, key, false);
            vnode = h("button", key, attrs, {disabled: disabled}, [
                node.busy ? h("span", "spin", {"class": "deck-spinner"}, null, "") :
                    (node.icon ? icon(node.icon, "", "icon") : null),
                h("span", "label", {"class": "deck-btn-label"}, null, label)
            ]);
        }
        vnode.btn = !isIcon;
        var out = [vnode];
        var confirming = !!node.confirm && ctx.local.confirming === confirmKey && !disabled && !node.busy;
        if (confirming) {
            var ask = confirmNode(node, confirmKey, rowId, key + "|ask");
            ask.ask = true;
            out.push(ask);
        }
        return out;
    }

    /*
     * A `switch` node (PROTOCOL 6.1, 2.1): a compact tile with a capsule in place of the icon and its code label.
     * The capsule shows the optimistic value while a click waits for the host's next tree (`local.pending`). The
     * vnode carries `btn: true`, so a switch joins the grid runs of its group like a tile.
     */
    function switchNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        var pending = ctx.local.pending || {};
        var on = pending.hasOwnProperty(node.id) ? !!pending[node.id] : !!node.value;
        var flash = ctx.local.flash ? ctx.local.flash[node.id] : null;
        var label = String(node.label || "");
        var short = node.short ? String(node.short) : label;
        var attrs = {"class": cls("deck-node deck-tile is-switch", on && "is-on", node.busy && "is-busy",
            disabled && "is-disabled", flash && FLASH_CLASS[flash]),
            "data-action": node.id, "data-kind": "switch", "data-value": on ? "true" : "false",
            role: "switch", "aria-checked": on ? "true" : "false", "aria-label": label};
        tipAttrs(node, attrs, key, short !== label);
        var vnode = h("button", key, attrs, {disabled: disabled}, [
            h("span", "cap", {"class": "deck-capsule"}, null, [h("i", "knob", {"class": "deck-capsule-knob"}, null, "")]),
            h("span", "text", {"class": "deck-tile-text"}, null, [
                h("span", "label", {"class": "deck-tile-label"}, null, short)
            ])
        ]);
        vnode.btn = true;
        return vnode;
    }

    function toggleNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        return h("label", key, tipAttrs(node, {"class": cls("deck-node deck-toggle", disabled && "is-disabled")}, key, false), null, [
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
        return h("label", key, tipAttrs(node, {"class": cls("deck-node deck-field", disabled && "is-disabled")}, key, false), null, [
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
        return h("div", key, tipAttrs(node, {"class": cls("deck-node deck-field", disabled && "is-disabled")}, key, false), null, [
            node.label ? h("span", "label", {"class": "deck-field-label"}, null, String(node.label)) : null,
            h("div", "row", {"class": "deck-input-row"}, null, row)
        ]);
    }

    function textNode(node, key) {
        var text = String(node.text === undefined ? "" : node.text);
        var attrs;
        if (node.chip) {
            attrs = tipAttrs(node, {"class": cls("deck-node deck-chip", tone(node.tone))}, key, false);
            return h("span", key, attrs, null, [
                h("i", "dot", {"class": "deck-chip-dot"}, null, ""),
                h("span", "text", {"class": "deck-chip-text"}, null, text)
            ]);
        }
        attrs = tipAttrs(node, {"class": cls("deck-node deck-text", tone(node.tone), node.small && "is-small",
            PATH_TEXT.test(text) && "is-path")}, key, false);
        return h("div", key, attrs, null, text);
    }

    function progressNode(node, ctx, key) {
        var indeterminate = node.value === null || node.value === undefined;
        var pct = indeterminate ? 100 : Math.max(0, Math.min(1, Number(node.value))) * 100;
        var disabled = ctx.offline;
        return h("div", key, tipAttrs(node, {"class": cls("deck-node deck-progress", indeterminate && "is-indeterminate")}, key, false), null, [
            h("div", "top", {"class": "deck-progress-top"}, null, [
                h("span", "label", {"class": "deck-progress-label"}, null, String(node.label || "")),
                node.cancel ? h("button", "stop", {"class": cls("deck-small-btn deck-stop", disabled && "is-disabled"),
                    "data-action": String(node.cancel), "data-kind": "button"}, {disabled: disabled}, [
                    icon("cross", "", "icon"),
                    h("span", "label", {}, null, "Stop")
                ]) : null
            ]),
            h("div", "track", {"class": "deck-progress-track"}, null, [
                h("div", "fill", {"class": "deck-progress-fill", style: "width: " + pct.toFixed(1) + "%"}, null, "")
            ]),
            node.detail ? h("div", "detail", {"class": "deck-progress-detail"}, null, [
                icon("chevron", "", "icon"),
                h("span", "text", {}, null, String(node.detail))
            ]) : null
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
                actions = actions.concat(buttonNodes(buttons[j], ctx, row.id, "a:" + buttons[j].id, false));
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
        return h("div", key, tipAttrs(node, {"class": "deck-node deck-list", "data-list": node.id}, key, false), null, children);
    }

    // The identity of a group: its id, else its title. Results lines and collapsed state are kept per group.
    function groupId(node) {
        if (node.id) {
            return String(node.id);
        }
        return "t:" + (node.title === undefined || node.title === null ? "" : String(node.title));
    }

    // The result line of a group or of a section (6.6): outcome icon, the text, the time, a chevron with a detail.
    // Every line is a notice that closes on a click (CONVENTIONS "Notices"); a line with a detail opens the
    // detail with the same click. The open detail stays until its own click, Escape or the group's next result.
    function resultLine(line, lineKey) {
        var hasDetail = !!line.detail;
        var inner = [
            icon(OUTCOME_ICON[line.outcome] || "info", "", "icon"),
            h("span", "text", {"class": "deck-g-result-text"}, null, String(line.text || "")),
            h("time", "time", {"class": "deck-g-result-time"}, null, clock(line.at)),
            hasDetail ? icon("chevron", "more", "more") : null
        ];
        return h("button", "line", {"class": cls("deck-g-result", tone(line.outcome), hasDetail && "has-detail"),
            "data-line": lineKey}, {}, inner);
    }

    // The explanation a failed line opened: one line, closed by a click on it.
    function detailLine(text, lineKey) {
        return h("button", "why", {"class": "deck-g-why", "data-why": lineKey}, {}, String(text));
    }

    // The result line and the open detail of a line key, as a list of vnodes.
    function resultNodes(ctx, lineKey) {
        var out = [];
        var line = ctx.local.lines && ctx.local.lines[lineKey];
        var why = ctx.local.detailOpen && ctx.local.detailOpen[lineKey];
        if (line) {
            out.push(resultLine(line, lineKey));
        }
        if (why) {
            out.push(detailLine(why, lineKey));
        }
        return out;
    }

    /*
     * A `group` (PROTOCOL 6.1): a caption and its items. With columns 2 or 3 every run of consecutive buttons
     * (and switches) becomes a grid of that many columns; with `tiles` the grid has four columns (three below
     * 360 px, CSS) whatever `columns` says; any other node takes the full width and ends the run. With `collapsible` the caption is the collapse control (chevron, the item count while collapsed).
     * Under the items the group keeps its result line.
     */
    function groupNode(node, ctx, key) {
        var tiles = !!node.tiles;
        var columns = Math.max(1, Math.min(3, Number(node.columns) || (tiles ? 3 : 1)));
        var gid = groupId(node);
        var collapsible = !!node.collapsible;
        var collapsed = collapsible && !!(ctx.local.collapsed && ctx.local.collapsed[gid]);
        var items = renderNodes(node.items, ctx, tiles);
        var count = 0;
        var i;
        for (i = 0; i < items.length; i++) {
            if (items[i].btn) {
                count++;
            }
        }
        var body = [];
        var run = [];
        var flush = function () {
            if (run.length) {
                var buttons = 0;
                for (var r = 0; r < run.length; r++) {
                    if (!run[r].ask) {
                        buttons++;
                    }
                }
                var cols = tiles ? TILE_COLUMNS : (buttons < 2 ? 1 : columns);
                body.push(h("div", "grid:" + run[0].k, {"class": cls("deck-grid", tiles && "is-tiles"), "data-cols": String(cols)}, null, run));
                run = [];
            }
        };
        for (i = 0; i < items.length; i++) {
            if ((columns >= 2 || tiles) && (items[i].btn || (items[i].ask && run.length))) {
                run.push(items[i]);
            } else {
                flush();
                body.push(items[i]);
            }
        }
        flush();
        body = body.concat(resultNodes(ctx, gid));
        var title = node.title === undefined || node.title === null ? "" : String(node.title);
        var head = null;
        if (title && collapsible) {
            head = h("button", "caption", {"class": "deck-g-head", "data-group": gid, "aria-expanded": collapsed ? "false" : "true"}, {}, [
                h("span", "title", {"class": "deck-caption"}, null, title),
                collapsed && count ? h("span", "count", {"class": "deck-count"}, null, String(count)) : null,
                icon("chevron", "chev", "chev")
            ]);
        } else if (title) {
            head = h("div", "caption", {"class": "deck-g-title deck-caption"}, null, title);
        }
        return h("section", key, {"class": cls("deck-node deck-group", tiles && "is-tiles", collapsed && "is-collapsed")}, null, [
            head,
            collapsed ? null : h("div", "items", {"class": "deck-g-body"}, null, body)
        ]);
    }

    function renderNodes(nodes, ctx, tiles) {
        var out = [];
        var groupSeen = {};
        for (var i = 0; i < (nodes || []).length; i++) {
            var node = nodes[i];
            if (!node || typeof node !== "object") {
                continue;
            }
            // Interactive nodes are keyed by id (they keep focus and scroll); a group by its id or title; the
            // rest by position and kind.
            var key = node.id ? "n:" + node.id : "p:" + i + ":" + node.kind;
            if (node.kind === "group") {
                var groupKey = "g:" + groupId(node);
                groupSeen[groupKey] = (groupSeen[groupKey] || 0) + 1;
                key = groupSeen[groupKey] > 1 ? groupKey + "#" + groupSeen[groupKey] : groupKey;
            }
            var vnode = null;
            switch (node.kind) {
            case "button":
                out = out.concat(buttonNodes(node, ctx, null, key, tiles));
                break;
            case "switch":
                vnode = switchNode(node, ctx, key);
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
                vnode = progressNode(node, ctx, key);
                break;
            case "list":
                vnode = listNode(node, ctx, key);
                break;
            case "row":
                vnode = rowNode(node, ctx, key);
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

    // A row: nodes side by side. A text beside an icon button is a header (the project name and its folder
    // button); a row that starts with a chip is a chip row.
    function rowNode(node, ctx, key) {
        var items = node.items || [];
        var head = false;
        var hasIconButton = false;
        for (var i = 0; i < items.length; i++) {
            if (items[i] && items[i].kind === "button" && items[i].variant === "icon") {
                hasIconButton = true;
            }
        }
        head = hasIconButton && items.length > 1 && items[0] && items[0].kind === "text" && !items[0].chip;
        var chips = !!(items[0] && items[0].kind === "text" && items[0].chip);
        return h("div", key, {"class": cls("deck-node deck-row", head && "is-head", chips && "is-chips")}, null,
            renderNodes(items, ctx, false));
    }

    // --- tabs, screens, frame ----------------------------------------------------------------------------------

    function selectedTab(sections, local) {
        for (var i = 0; i < sections.length; i++) {
            if (sections[i].id === local.tab) {
                return i;
            }
        }
        return sections.length ? 0 : -1;
    }

    function tabsNode(sections, index) {
        var tabs = [h("span", "thumb", {"class": "deck-tabs-thumb"}, null, "")];
        for (var i = 0; i < sections.length; i++) {
            var section = sections[i];
            tabs.push(h("button", "t:" + section.id, {"class": cls("deck-tab", i === index && "is-on"), "data-tab": section.id,
                role: "tab", "aria-selected": i === index ? "true" : "false"}, {}, [
                section.icon ? icon(section.icon, "", "icon") : null,
                h("span", "label", {}, null, String(section.title || ""))
            ]));
        }
        return h("div", "tabs", {"class": "deck-tabs", role: "tablist", style: "--n: " + sections.length + "; --i: " + Math.max(0, index),
            "data-on": index >= 0 ? sections[index].id : ""}, null, tabs);
    }

    function statusLine(status) {
        return h("div", "status", {"class": cls("deck-status", tone(status.tone))}, null, [
            h("i", "dot", {"class": "deck-status-dot"}, null, ""),
            h("span", "text", {}, null, String(status.text || ""))
        ]);
    }

    // The selected tab's content: its status line, its nodes, the section-level result line.
    function tabPanel(section, ctx) {
        var nodes = renderNodes(section.items, ctx, false);
        var items = section.items || [];
        var last = items[items.length - 1];
        var dock = null;
        if (last && last.kind === "row" && nodes.length) {
            var allButtons = (last.items || []).length > 0;
            for (var i = 0; i < (last.items || []).length; i++) {
                if (!last.items[i] || last.items[i].kind !== "button") {
                    allButtons = false;
                }
            }
            if (allButtons) {
                dock = nodes.pop();   // a button row that ends a tab sticks to its foot
                dock.a["class"] += " is-dock";
            }
        }
        return h("div", "tab:" + section.id, {"class": "deck-tabpanel"}, null, [
            section.status ? statusLine(section.status) : null
        ].concat(nodes, resultNodes(ctx, "~" + section.id), dock ? [dock] : []));
    }

    // The connecting screen (docs/PANEL.md 2): the glyph with a pulsing dot, the title, after 15 s a soft second
    // line and, when deck.json names a host exe, the start link.
    function connectingScreen(local) {
        var parts = [h("div", "gl", {"class": "deck-gl"}, null, [glyph(64), h("span", "pulse", {"class": "deck-pulse"}, null, "")])];
        if (local.connection === "protocol") {
            parts.push(h("h1", "title", {"class": "deck-h1"}, null, "Update Premiere Deck"));
            if (local.hostVersion) {
                parts.push(h("p", "line", {"class": "deck-soft"}, null, "Host version " + local.hostVersion));
            }
        } else {
            parts.push(h("h1", "title", {"class": "deck-h1"}, null, "Connecting to Host"));
            if (local.late) {
                if (!local.hintClosed) {
                    parts.push(h("p", "line", {"class": "deck-soft is-notice", "data-dismiss": "hint"}, null,
                        "Start Premiere Deck on this computer"));
                }
                if (local.hostExe) {
                    parts.push(h("button", "start", {"class": "deck-btn has-icon deck-start", "data-start-host": "1"}, {}, [
                        icon("power", "", "icon"),
                        h("span", "label", {"class": "deck-btn-label"}, null, "Start Premiere Deck")
                    ]));
                }
                if (local.startStatus) {
                    parts.push(h("p", "result", {"class": "deck-soft is-small is-notice", "data-dismiss": "start"}, null,
                        String(local.startStatus)));
                }
            }
        }
        return h("div", "connecting", {"class": "deck-connecting"}, null, parts);
    }

    function menuNode(local, tree) {
        var menu = local.menu;
        if (!menu) {
            return null;
        }
        var button = findButton(tree, menu.buttonId);
        if (!button || !button.node.menu) {
            return null;
        }
        var spec = button.node.menu;
        var items = [];
        var options = spec.options || [];
        for (var i = 0; i < options.length; i++) {
            var value = options[i].value;
            var on = String(value) === String(spec.value);
            items.push(h("button", "o:" + String(value), {"class": cls("deck-menu-item", on && "is-on"), role: "menuitemradio",
                "aria-checked": on ? "true" : "false", "data-menu-pick": String(value), "data-menu-id": spec.id}, {}, [
                h("span", "mark", {"class": "deck-menu-mark"}, null, [on ? icon("check", "", "icon") : null]),
                SWATCH.test(String(options[i].swatch || "")) ? h("i", "sw", {"class": "deck-menu-swatch",
                    style: "background-color: " + options[i].swatch}, null, "") : null,
                h("span", "label", {}, null, String(options[i].label))
            ]));
        }
        return h("div", "menu", {"class": cls("deck-menu", menu.ready && "is-ready", menu.above && "is-above"),
            style: menu.style || "left: 0px; top: 0px", role: "menu"}, null, items);
    }

    function tipNode(local) {
        var tip = local.tip;
        if (!tip) {
            return null;
        }
        return h("div", "tip", {"class": cls("deck-tip", tip.ready && "is-ready", tip.below && "is-below"), role: "tooltip",
            style: tip.style || "left: 0px; top: 0px"}, null, [
            tip.label ? h("b", "label", {}, null, tip.label) : null,
            tip.hint ? h("span", "hint", {}, null, tip.hint) : null,
            tip.meta ? h("span", "meta", {"class": "deck-tip-meta"}, null, tip.meta) : null,
            h("i", "arrow", {"class": "deck-tip-arrow", style: "left: " + (tip.arrow || 24) + "px"}, null, "")
        ]);
    }

    function footerNode(tree, local, connected) {
        var footer = tree && tree.footer;
        var text = footer ? String(footer.text || "") : "Premiere Deck" + (local.panelVersion ? " " + local.panelVersion : "");
        var link = footer && footer.link && footer.link.action ? footer.link : {label: "Open host", action: ""};
        return h("footer", "footer", {"class": "deck-footer"}, null, [
            h("span", "text", {"class": "deck-ver"}, null, text),
            h("button", "link", {"class": cls("deck-small-btn deck-link", !connected && "is-disabled"), "data-action": link.action || "host.open",
                "data-kind": "link"}, {disabled: !connected}, [
                icon("open", "", "icon"),
                h("span", "label", {}, null, String(link.label || "Open host"))
            ])
        ]);
    }

    /*
     * The whole panel as a vnode. `local` is the display's own state:
     *   connection   "connecting" | "connected" | "restart_pending" | "protocol"
     *   late         true after 15 s without a host (the soft second line of the connecting screen)
     *   hostExe      host_exe from deck.json when that file exists, else null
     *   startStatus  result line of the last "Start Premiere Deck" click or null
     *   tab          id of the selected section (the tab), remembered in localStorage
     *   collapsed    {groupId: bool} remembered per collapsible group
     *   flash        {buttonId: outcome} the tiles that show a result right now (2 s)
     *   lines        {groupId | "~" + sectionId: {outcome, text, detail, at}} the result lines (3 s / 10 s)
     *   detailOpen   {line key: detail text} the explanations a click on a failed line opened
     *   pending      {switchId: bool} the value a clicked switch shows until the host's next tree
     *   hintClosed   the connecting screen's "Start Premiere Deck on this computer" line was clicked away
     *   restartClosed the restart notice was clicked away
     *   confirming   key of the button whose confirmation is open, or null
     *   menu         {buttonId, style, ready, above} the open tile menu, or null
     *   tip          {label, hint, style, arrow, ready, below} the open tooltip, or null
     *   toast        {text, tone} or null
     *   hostVersion  host version for the protocol notice
     *   panelVersion the panel's own version for the footer while no tree is there
     */
    function renderTree(tree, local) {
        local = local || {};
        var connected = local.connection === "connected" || local.connection === "restart_pending";
        var ctx = {local: local, offline: !connected, tree: tree};
        var screen = !tree || local.connection === "protocol";
        var sections = (tree && tree.sections) || [];
        var children = [h("div", "scene", {"class": "deck-scene", "aria-hidden": "true"}, null, [
            h("i", "f1", {"class": "f1"}, null, ""), h("i", "f2", {"class": "f2"}, null, ""), h("i", "f3", {"class": "f3"}, null, "")
        ])];

        if (screen) {
            children.push(h("main", "body", {"class": "deck-body is-screen"}, null, [connectingScreen(local)]));
            children.push(footerNode(null, local, false));
        } else {
            var index = selectedTab(sections, local);
            var dot = tree.status ? String(tree.status.text || "") : "";
            var dotTone = connected ? "ok" : "warn";
            children.push(h("header", "header", {"class": "deck-top"}, null, [
                glyph(26),
                sections.length ? tabsNode(sections, index) : h("div", "title", {"class": "deck-title"}, null, String((tree && tree.title) || "Premiere Deck")),
                h("span", "conn", tipAttrs({label: connected ? "Connected" : "Connecting to Host", hint: dot},
                    {"class": cls("deck-conn", tone(dotTone)), role: "img", "aria-label": connected ? "Connected" : "Connecting to Host"}, "conn", true), null, [
                    h("i", "dot", {"class": "deck-conn-dot"}, null, "")
                ])
            ]));
            if (local.connection === "restart_pending" && !local.restartClosed) {
                children.push(h("div", "notice", {"class": "deck-notice tone-warn", "data-dismiss": "restart"}, null,
                    "Restart Premiere Pro to finish the Premiere Deck update"));
            }
            children.push(h("main", "body", {"class": cls("deck-body", !connected && "is-offline")}, null,
                index >= 0 ? [tabPanel(sections[index], ctx)] : []));
            children.push(footerNode(tree, local, connected));
        }
        if (local.toast) {
            children.push(h("div", "toast", {"class": cls("deck-toast", tone(local.toast.tone)), "data-dismiss": "toast"}, null,
                String(local.toast.text || "")));
        }
        var menu = menuNode(local, tree);
        if (menu) {
            children.push(menu);
        }
        var tip = tipNode(local);
        if (tip) {
            children.push(tip);
        }
        return h("div", "root", {"class": "deck"}, null, children);
    }

    // --- tree lookups, results ---------------------------------------------------------------------------------

    // The button or switch node `id` with the section and group that hold it: {node, section, group} or null. A
    // button in a list row is found as well (its group is null).
    function findButton(tree, id) {
        var sections = (tree && tree.sections) || [];
        var found = null;
        var visit = function (nodes, section, group) {
            for (var i = 0; i < (nodes || []).length && !found; i++) {
                var node = nodes[i];
                if (!node) {
                    continue;
                }
                if ((node.kind === "button" || node.kind === "switch") && node.id === id) {
                    found = {node: node, section: section, group: group};
                } else if (node.kind === "row") {
                    visit(node.items, section, group);
                } else if (node.kind === "group") {
                    visit(node.items, section, node);
                } else if (node.kind === "list") {
                    for (var r = 0; r < (node.rows || []).length && !found; r++) {
                        var actions = node.rows[r].actions || [];
                        for (var a = 0; a < actions.length; a++) {
                            if (actions[a].id === id) {
                                found = {node: actions[a], section: section, group: null};
                            }
                        }
                    }
                }
            }
        };
        for (var s = 0; s < sections.length && !found; s++) {
            visit(sections[s].items, sections[s], null);
        }
        return found;
    }

    // The key of the result line a result for button `id` belongs to (the group's id, else the section's), or
    // null when the button is not in the tree.
    function lineKeyFor(tree, id) {
        var found = findButton(tree, id);
        if (!found) {
            return null;
        }
        return found.group ? groupId(found.group) : "~" + found.section.id;
    }

    // Every line key the tree can hold: its groups and its sections.
    function lineKeys(tree) {
        var keys = {};
        var sections = (tree && tree.sections) || [];
        var visit = function (nodes) {
            for (var i = 0; i < (nodes || []).length; i++) {
                var node = nodes[i];
                if (node && node.kind === "group") {
                    keys[groupId(node)] = true;
                } else if (node && node.kind === "row") {
                    visit(node.items);
                }
            }
        };
        for (var s = 0; s < sections.length; s++) {
            keys["~" + sections[s].id] = true;
            visit(sections[s].items);
        }
        return keys;
    }

    function headerStatus(tree, local) {
        if (local.connection === "restart_pending") {
            return {text: "Update available · Restart Premiere Pro", tone: "warn"};
        }
        if (local.connection === "protocol") {
            return {text: "Update Premiere Deck" + (local.hostVersion ? " (host " + local.hostVersion + ")" : ""), tone: "error"};
        }
        if (local.connection !== "connected") {
            return {text: "Connecting to Host", tone: "neutral"};
        }
        if (tree && tree.status) {
            return tree.status;
        }
        return {text: "Connected", tone: "ok"};
    }

    // --- geometry ------------------------------------------------------------------------------------------------

    /*
     * Where a floating card (tooltip, menu) goes. `anchor` {left, top, width, height} is the element, `size` {w, h}
     * the card, `bounds` {w, h} the panel, all in panel pixels. The card is centred on the element (`align`
     * "center", the tooltip) or hangs from its right edge (`align` "end", the menu), and stays inside the 12 px
     * gutter: in the first and last grid column it therefore lines up with the tile's outer edge. It goes above
     * the element and flips below when there is no room above. `arrow` is the nub's x inside the card.
     */
    function placeFloating(anchor, size, bounds, align) {
        var wanted = align === "end" ? anchor.left + anchor.width - size.w : anchor.left + anchor.width / 2 - size.w / 2;
        var left = Math.min(Math.max(wanted, EDGE), Math.max(EDGE, bounds.w - EDGE - size.w));
        var above = anchor.top - size.h - GAP;
        var below = anchor.top + anchor.height + GAP;
        var flip = above < 4;
        if (align === "end") {
            flip = below + size.h <= bounds.h - 4;   // a menu opens downwards when it fits
        }
        var top = Math.max(4, flip ? below : above);
        var arrow = Math.min(Math.max(anchor.left + anchor.width / 2 - left, 14), Math.max(14, size.w - 14));
        return {left: Math.round(left), top: Math.round(top), below: flip, arrow: Math.round(arrow)};
    }

    /*
     * How a theme change is drawn: "instant" for the first theme of a page load, an unchanged theme and reduced
     * motion; "sweep" where the View Transitions API exists; "fade" (the 300 ms CSS crossfade) elsewhere, which is
     * CEP's CEF 99 (docs/PANEL.md 3).
     */
    function planThemeSwitch(env) {
        if (!env.known || env.current === env.next || env.reducedMotion) {
            return "instant";
        }
        return env.hasViewTransitions ? "sweep" : "fade";
    }

    function normaliseTheme(name) {
        return THEMES.hasOwnProperty(name) ? name : DEFAULT_THEME;
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
        var el = SVG_TAGS[vnode.t] && doc.createElementNS ? doc.createElementNS(SVG_NS, vnode.t) : doc.createElement(vnode.t);
        el.__deckKey = childKey(vnode);
        for (var name in vnode.a) {
            if (vnode.a.hasOwnProperty(name)) {
                el.setAttribute(name, vnode.a[name]);
            }
        }
        if (vnode.x !== null) {
            if (vnode.x !== "") {
                el.textContent = vnode.x;
            }
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

    function storageGet(win, key) {
        try {
            return win.localStorage.getItem(key);
        } catch (e) {
            return null;
        }
    }

    function storageSet(win, key, value) {
        try {
            win.localStorage.setItem(key, value);
        } catch (e) {
            // Not remembered without storage.
        }
    }

    function loadCollapsed(win) {
        var result = {};
        try {
            for (var i = 0; i < win.localStorage.length; i++) {
                var key = win.localStorage.key(i);
                if (key && key.indexOf(KEY_GROUP) === 0) {
                    result[key.substring(KEY_GROUP.length)] = win.localStorage.getItem(key) === "1";
                }
            }
        } catch (e) {
            // No storage: groups start expanded.
        }
        return result;
    }

    // `win` is the window the display lives in (the page's own; tests pass a stand-in).
    function Display(container, win) {
        this.container = container;
        this.win = win || root;
        this.common = this.win.DeckCommon;
        this.tree = null;
        this.vnode = null;
        this.local = {
            connection: "connecting",
            late: false,
            hostExe: null,
            startStatus: null,
            tab: storageGet(this.win, KEY_TAB),
            collapsed: loadCollapsed(this.win),
            flash: {},
            lines: {},
            detailOpen: {},
            pending: {},
            hintClosed: false,
            restartClosed: false,
            confirming: null,
            menu: null,
            tip: null,
            toast: null,
            hostVersion: "",
            panelVersion: ""
        };
        this.theme = null;
        this.themeSeen = false;
        this.toastTimer = null;
        this.lateTimer = null;
        this.resizeTimer = null;
        this.tipTimer = null;
        this.flashTimers = {};
        this.lineTimers = {};
        this.fadeTimer = null;
        this.client = null;
    }

    Display.prototype.render = function () {
        var next = renderTree(this.tree, this.local);
        var ops = diff(this.vnode, next);
        applyOps(this.container, ops);
        this.vnode = next;
        this.checkFloating();
    };

    // A tooltip or menu whose element is gone after a re-render is closed; a card that was just opened is measured
    // and placed (two renders: the first draws it hidden, the second at its place).
    Display.prototype.checkFloating = function () {
        var local = this.local;
        var box = this.container;
        if (local.tip) {
            var target = box.querySelector("[data-tip=\"" + cssEscape(local.tip.key) + "\"]");
            if (!target) {
                local.tip = null;
                this.render();
                return;
            }
            if (!local.tip.ready) {
                this.placeCard(local.tip, target, box.querySelector(".deck-tip"), "center");
                return;
            }
        }
        if (local.menu) {
            var opener = box.querySelector("[data-action=\"" + cssEscape(local.menu.buttonId) + "\"]");
            if (!opener || !box.querySelector(".deck-menu")) {
                local.menu = null;
                this.render();
                return;
            }
            if (!local.menu.ready) {
                this.placeCard(local.menu, opener, box.querySelector(".deck-menu"), "end");
            }
        }
    };

    Display.prototype.placeCard = function (state, target, card, align) {
        if (!card) {
            return;
        }
        var box = this.container.getBoundingClientRect();
        var r = target.getBoundingClientRect();
        var spot = placeFloating({left: r.left - box.left, top: r.top - box.top, width: r.width, height: r.height},
            {w: card.offsetWidth, h: card.offsetHeight}, {w: box.width, h: box.height}, align);
        state.style = "left: " + spot.left + "px; top: " + spot.top + "px";
        state.below = spot.below;
        state.above = !spot.below;
        state.arrow = spot.arrow;
        state.ready = true;
        this.render();
    };

    function cssEscape(text) {
        return String(text).replace(/(["\\])/g, "\\$1");
    }

    Display.prototype.start = function () {
        var self = this;
        this.local.panelVersion = this.common.panelVersion ? this.common.panelVersion() : "";
        var current = this.win.document.documentElement.getAttribute("data-theme");
        this.theme = normaliseTheme(current);
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
        this.loadSprite();
        this.bindEvents();
        this.armLate();
        this.render();
        this.client.start();
    };

    // The icon sprite is inlined once: <use> cannot reference an external file from a file: page.
    Display.prototype.loadSprite = function () {
        var doc = this.win.document;
        var win = this.win;
        var attach = function (text) {
            if (!text || doc.getElementById("deck-sprite")) {
                return;
            }
            var holder = doc.createElement("div");
            holder.innerHTML = text;
            var svg = holder.querySelector("svg");
            if (svg) {
                svg.setAttribute("id", "deck-sprite");
                doc.body.insertBefore(svg, doc.body.firstChild);
            }
        };
        var request = new win.XMLHttpRequest();
        request.onload = function () {
            if (request.status === 200 || (request.status === 0 && request.responseText)) {
                attach(request.responseText);
            }
        };
        request.onerror = function () {
            var cs = win.DeckCommon;
            var text = cs && cs.readText ? cs.readText(cs.joinPath(cs.extensionDir(), "icons.svg")) : null;
            attach(text);
        };
        try {
            request.open("GET", "icons.svg", true);
            request.send();
        } catch (e) {
            request.onerror();
        }
    };

    Display.prototype.armLate = function () {
        var self = this;
        if (this.lateTimer) {
            return;
        }
        this.lateTimer = setTimeout(function () {
            self.lateTimer = null;
            if (self.local.connection !== "connected" && self.local.connection !== "restart_pending") {
                var deck = self.common.readDeck();
                var exe = deck && deck.host_exe ? String(deck.host_exe) : "";
                self.local.late = true;
                // Outside Premiere (a dev browser) the exe cannot be checked; the click answers "not found".
                self.local.hostExe = exe && (self.common.fileExists(exe) || self.common.isDevBrowser()) ? exe : null;
                self.render();
            }
        }, LATE_MS);
    };

    Display.prototype.onState = function (state, info) {
        this.local.connection = state;
        this.local.hostVersion = info && info.host_version ? info.host_version : this.local.hostVersion;
        if (state === "connected" || state === "restart_pending") {
            this.local.late = false;
            this.local.hintClosed = false;
            this.local.startStatus = null;
            if (this.lateTimer) {
                clearTimeout(this.lateTimer);
                this.lateTimer = null;
            }
        } else {
            this.local.confirming = null;
            this.local.menu = null;
            this.local.tip = null;
            this.armLate();
        }
        this.render();
    };

    Display.prototype.onMessage = function (message) {
        if (message.type === "ui") {
            this.tree = message;
            this.local.pending = {};   // the host's tree is the truth for every switch
            this.pruneResults();
            if (this.local.confirming && !this.confirmStillValid()) {
                this.local.confirming = null;
            }
            this.render();
        } else if (message.type === "toast") {
            this.showToast(message);
        } else if (message.type === "theme") {
            this.setTheme(message.name);
        } else if (message.type === "action_result") {
            this.onResult(message);
        }
    };

    // Result lines go when their group leaves the tree.
    Display.prototype.pruneResults = function () {
        var keys = lineKeys(this.tree);
        var name;
        for (name in this.local.lines) {
            if (this.local.lines.hasOwnProperty(name) && !keys[name]) {
                this.closeLine(name);
            }
        }
        for (name in this.local.detailOpen) {
            if (this.local.detailOpen.hasOwnProperty(name) && !keys[name]) {
                delete this.local.detailOpen[name];
            }
        }
    };

    // Removes a result line and its timer (the open detail, if any, stays).
    Display.prototype.closeLine = function (key) {
        if (this.lineTimers[key]) {
            clearTimeout(this.lineTimers[key]);
            delete this.lineTimers[key];
        }
        delete this.local.lines[key];
    };

    // `action_result` (PROTOCOL 6.6): the tile shows the outcome for 2 s, the group shows the line for 3 s (ok,
    // neutral) or 10 s (warn, error). A result for a button that is not in the tree changes nothing.
    Display.prototype.onResult = function (message) {
        var self = this;
        var id = String(message.id || "");
        var key = lineKeyFor(this.tree, id);
        if (key === null) {
            return;
        }
        var outcome = FLASH_CLASS.hasOwnProperty(message.outcome) ? message.outcome : "neutral";
        this.local.flash[id] = outcome;
        this.local.lines[key] = {outcome: outcome, text: String(message.text || ""), detail: message.detail ? String(message.detail) : null,
            at: Number(message.at) || Date.now()};
        delete this.local.detailOpen[key];
        if (this.lineTimers[key]) {
            clearTimeout(this.lineTimers[key]);
        }
        this.lineTimers[key] = setTimeout(function () {
            delete self.lineTimers[key];
            delete self.local.lines[key];
            self.render();
        }, outcome === "ok" || outcome === "neutral" ? LINE_MS : LINE_ERROR_MS);
        if (this.flashTimers[id]) {
            clearTimeout(this.flashTimers[id]);
        }
        this.flashTimers[id] = setTimeout(function () {
            delete self.flashTimers[id];
            delete self.local.flash[id];
            self.render();
        }, RESULT_MS);
        this.render();
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

    Display.prototype.closeToast = function () {
        if (this.toastTimer) {
            clearTimeout(this.toastTimer);
            this.toastTimer = null;
        }
        this.local.toast = null;
    };

    // A click on a notice closes it (CONVENTIONS "Notices"): the toast, the restart notice, the connecting hints.
    Display.prototype.dismiss = function (what) {
        if (what === "toast") {
            this.closeToast();
        } else if (what === "restart") {
            this.local.restartClosed = true;
        } else if (what === "hint") {
            this.local.hintClosed = true;
        } else if (what === "start") {
            this.local.startStatus = null;
        }
        this.render();
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

    // --- theme (PROTOCOL 6.5, PANEL 3) ---

    Display.prototype.setTheme = function (name) {
        var self = this;
        var next = normaliseTheme(name);
        var win = this.win;
        var html = win.document.documentElement;
        var mode = planThemeSwitch({
            known: this.themeSeen,
            current: this.theme,
            next: next,
            reducedMotion: !!(win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches),
            hasViewTransitions: typeof win.document.startViewTransition === "function"
        });
        this.themeSeen = true;
        this.theme = next;
        storageSet(win, KEY_THEME, next);
        var apply = function () {
            html.setAttribute("data-theme", next);
        };
        if (mode === "sweep") {
            var line = null;
            var transition = win.document.startViewTransition(function () {
                apply();
                line = win.document.createElement("div");
                line.className = "theme-line";
                win.document.body.appendChild(line);
            });
            var done = function () {
                if (line && line.parentNode) {
                    line.parentNode.removeChild(line);
                }
            };
            if (transition && transition.finished && transition.finished.then) {
                transition.finished.then(done, done);
            }
        } else if (mode === "fade") {
            html.classList.add("theme-fade");
            apply();
            if (this.fadeTimer) {
                clearTimeout(this.fadeTimer);
            }
            this.fadeTimer = setTimeout(function () {
                self.fadeTimer = null;
                html.classList.remove("theme-fade");
            }, FADE_MS + 40);
        } else {
            apply();
        }
    };

    // --- actions ---

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
        this.client.send({type: "display.ready", size: {w: this.win.innerWidth, h: this.win.innerHeight}});
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

    // --- tooltip and menu ---

    Display.prototype.hideTip = function () {
        if (this.tipTimer) {
            clearTimeout(this.tipTimer);
            this.tipTimer = null;
        }
        if (this.local.tip) {
            this.local.tip = null;
            this.render();
        }
    };

    Display.prototype.armTip = function (el) {
        var self = this;
        if (this.tipTimer) {
            clearTimeout(this.tipTimer);
        }
        this.tipTimer = setTimeout(function () {
            self.tipTimer = null;
            if (self.local.menu || !el.parentNode) {
                return;
            }
            self.local.tip = {key: el.getAttribute("data-tip"), label: el.getAttribute("data-tip-label") || "",
                hint: el.getAttribute("data-tip-hint") || "",
                meta: el.getAttribute("data-tip-meta") || "", ready: false, style: "left: 0px; top: 0px"};
            self.render();
        }, TIP_DELAY_MS);
    };

    Display.prototype.openMenu = function (buttonId) {
        this.hideTip();
        this.local.menu = {buttonId: buttonId, ready: false, above: false, style: "left: 0px; top: 0px"};
        this.render();
    };

    Display.prototype.closeMenu = function () {
        if (this.local.menu) {
            this.local.menu = null;
            this.render();
        }
    };

    Display.prototype.selectTab = function (id) {
        this.local.tab = id;
        storageSet(this.win, KEY_TAB, id);
        this.hideTip();
        this.render();
    };

    Display.prototype.toggleGroup = function (id) {
        var now = !this.local.collapsed[id];
        this.local.collapsed[id] = now;
        storageSet(this.win, KEY_GROUP + id, now ? "1" : "0");
        this.hideTip();
        this.render();
    };

    Display.prototype.bindEvents = function () {
        var self = this;
        var box = this.container;
        box.addEventListener("click", function (event) {
            var target = event.target;
            var el;
            if (self.local.menu) {
                var inMenu = closest(target, "data-menu-pick", box);
                var opener = closest(target, "data-menu-open", box);
                if (!inMenu && !(opener && opener.getAttribute("data-menu-open") === self.local.menu.buttonId)) {
                    self.closeMenu();   // a click outside only closes the menu
                    return;
                }
            }
            self.hideTip();
            if ((el = closest(target, "data-menu-pick", box))) {
                var menuId = el.getAttribute("data-menu-id");
                var value = el.getAttribute("data-menu-pick");
                self.local.menu = null;
                self.render();
                self.sendAction(menuId, value);
                return;
            }
            if ((el = closest(target, "data-menu-open", box))) {
                if (self.local.menu) {
                    self.closeMenu();
                } else if (!self.isOffline()) {
                    self.openMenu(el.getAttribute("data-menu-open"));
                }
                return;
            }
            if ((el = closest(target, "data-tab", box))) {
                self.selectTab(el.getAttribute("data-tab"));
                return;
            }
            if ((el = closest(target, "data-group", box))) {
                self.toggleGroup(el.getAttribute("data-group"));
                return;
            }
            if ((el = closest(target, "data-line", box))) {
                // A click closes the line; a line with a detail opens the detail in the same click.
                var lineKey = el.getAttribute("data-line");
                var line = self.local.lines[lineKey];
                self.closeLine(lineKey);
                if (line && line.detail) {
                    self.local.detailOpen[lineKey] = line.detail;
                }
                self.render();
                return;
            }
            if ((el = closest(target, "data-why", box))) {
                delete self.local.detailOpen[el.getAttribute("data-why")];
                self.render();
                return;
            }
            if ((el = closest(target, "data-dismiss", box))) {
                self.dismiss(el.getAttribute("data-dismiss"));
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
            if (kind !== "button" && kind !== "link" && kind !== "switch") {
                return;
            }
            if (el.disabled || el.className.indexOf("is-busy") >= 0 || el.className.indexOf("is-disabled") >= 0) {
                return;
            }
            if (kind === "switch") {
                self.flipSwitch(el.getAttribute("data-action"), el.getAttribute("data-value") !== "true");
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
        box.addEventListener("contextmenu", function (event) {
            var el = closest(event.target, "data-action", box);
            var badge = el ? el.querySelector("[data-menu-open]") : null;
            if (badge && !self.isOffline()) {
                event.preventDefault();
                self.openMenu(badge.getAttribute("data-menu-open"));
            }
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
            } else if (event.key === "Escape") {
                if (self.local.menu) {
                    self.closeMenu();
                } else if (self.local.tip) {
                    self.hideTip();
                } else if (self.closeDetails()) {
                    self.render();
                } else if (self.local.confirming) {
                    self.local.confirming = null;
                    self.render();
                }
            }
        });
        // Tooltip: 400 ms of hover, or keyboard focus; gone on leave, press and scroll.
        box.addEventListener("mouseover", function (event) {
            var el = closest(event.target, "data-tip", box);
            if (!el) {
                return;
            }
            if (self.local.tip && self.local.tip.key === el.getAttribute("data-tip")) {
                return;
            }
            if (self.local.tip) {
                self.hideTip();
            }
            self.armTip(el);
        });
        box.addEventListener("mouseout", function (event) {
            var el = closest(event.target, "data-tip", box);
            if (!el || (event.relatedTarget && el.contains && el.contains(event.relatedTarget))) {
                return;
            }
            self.hideTip();
        });
        box.addEventListener("mousedown", function () {
            self.hideTip();
        });
        box.addEventListener("focusin", function (event) {
            var el = closest(event.target, "data-tip", box);
            var visible = false;
            try {
                visible = !!el && el.matches(":focus-visible");
            } catch (e) {
                visible = false;
            }
            if (el && visible) {
                self.armTip(el);
            }
        });
        box.addEventListener("focusout", function () {
            self.hideTip();
        });
        box.addEventListener("scroll", function () {
            self.hideTip();
            self.closeMenu();
        }, true);
        this.win.addEventListener("resize", function () {
            self.hideTip();
            self.closeMenu();
            if (self.resizeTimer) {
                clearTimeout(self.resizeTimer);
            }
            self.resizeTimer = setTimeout(function () {
                self.resizeTimer = null;
                self.sendReady();
            }, RESIZE_DEBOUNCE_MS);
        });
    };

    // A switch click (PROTOCOL 6.1): the capsule flips at once and the action asks for the new value; the host's
    // next tree replaces the optimistic value (a refused change comes back as the old one).
    Display.prototype.flipSwitch = function (id, value) {
        if (this.isOffline()) {
            return;
        }
        this.local.pending[id] = value;
        this.render();
        this.sendAction(id, value);
    };

    Display.prototype.isOffline = function () {
        return this.local.connection !== "connected" && this.local.connection !== "restart_pending";
    };

    // Closes every open result detail; true when one was open.
    Display.prototype.closeDetails = function () {
        var any = false;
        for (var key in this.local.detailOpen) {
            if (this.local.detailOpen.hasOwnProperty(key)) {
                delete this.local.detailOpen[key];
                any = true;
            }
        }
        return any;
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
        headerStatus: headerStatus,
        placeFloating: placeFloating,
        planThemeSwitch: planThemeSwitch,
        normaliseTheme: normaliseTheme,
        findButton: findButton,
        lineKeyFor: lineKeyFor,
        lineKeys: lineKeys,
        groupId: groupId,
        selectedTab: selectedTab,
        clock: clock,
        Display: Display,
        LATE_MS: LATE_MS,
        RESULT_MS: RESULT_MS,
        LINE_MS: LINE_MS,
        LINE_ERROR_MS: LINE_ERROR_MS,
        TILE_COLUMNS: TILE_COLUMNS,
        TIP_DELAY_MS: TIP_DELAY_MS
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
