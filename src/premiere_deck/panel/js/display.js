/*
 * Premiere Deck panel: the visible display (docs/PANEL.md 2, docs/PROTOCOL.md 6).
 * Renders the UI tree the host sends and sends the user's actions back. It knows no module and no action id.
 *
 * Rendering is split into pure functions, exported for `node tests/panel/run.js`:
 *   renderTree(tree, local) -> vnode           the whole panel as plain data
 *   diff(oldVnode, newVnode) -> operations      what to change in the DOM (keyed by node id)
 *   placeFloating(...), planThemeSwitch(...)    geometry of the floating cards, the theme switch mode
 * and one impure applier (applyOps) that touches the DOM. Event handling is delegated through data attributes,
 * so vnodes stay plain data. The sprite icons are drawn as <svg><use href="#i-<name>"/></svg>; the sprite
 * (icons.svg) is inlined into the page at start.
 *
 * Layout (3.0, "Lanes"): one fixed stage of 360 x 640 CSS px, scaled as a whole by the footer slider (CSS `zoom`).
 * Every titled tile group is a lane: its code at the left edge and fixed-size tiles; the pinned row sits on top.
 */
(function (root) {
    "use strict";

    var SVG_NS = "http://www.w3.org/2000/svg";
    var SVG_TAGS = {svg: true, use: true};
    var LATE_MS = 15000;          // the soft second line of the connecting screen
    var RESULT_MS = 2000;         // an action result stays on its tile this long
    var LINE_MS = 3000;           // a success or neutral result line hides itself after this long
    var LINE_ERROR_MS = 10000;    // an error or warn result line
    var CONFIRM_MS = 4000;        // an armed tile flips back after this long without a press
    var TIP_DELAY_MS = 400;
    var FADE_MS = 300;            // the CSS crossfade of a theme switch where View Transitions are missing
    var RESIZE_DEBOUNCE_MS = 300;
    var STAGE_W = 360;            // the fixed stage (docs/PANEL.md 3): the layout never follows the window width
    var STAGE_H = 640;
    var SCALE_MIN = 80;           // the footer slider, in per cent of the stage
    var SCALE_MAX = 150;
    var SCALE_STEP = 5;
    var SCALE_DEFAULT = 100;
    var SCALE_HIDPI = 125;        // the first start on a screen scaled 125 % or more
    var PINS_MAX = 5;             // the pinned row when the tree names no `pins.max`
    var LANE_COLS = 6;            // tiles per lane row (the stage is fixed, PANEL.md 3)
    var PINS_LINE = "~pins";      // the result line of the pinned row
    var KEY_GROUP = "deck.group.";
    var KEY_TAB = "deck.tab";
    var KEY_THEME = "deck.theme";
    var KEY_SCALE = "deck.scale";
    var EDGE = 12;                // the stage's side gutter: floating cards stay inside it
    var GAP = 8;
    var DEFAULT_THEME = "black";
    var THEMES = {black: true, sky: true};
    var FLASH_CLASS = {ok: "is-done", error: "is-failed", warn: "is-warned", neutral: "is-noted"};
    var OUTCOME_ICON = {ok: "check", error: "cross", warn: "warn", neutral: "info"};
    var PATH_TEXT = /^([A-Za-z]:[\\\/]|\\\\)/;
    var SWATCH = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;   // a menu option's `swatch` colour (PROTOCOL 6.1)
    var VERSION_TEXT = /(\d+\.\d+\.\d+[^\s()]*)/;

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

    function text(value) {
        return value === undefined || value === null ? "" : String(value);
    }

    // The tooltip of an element (docs/PANEL.md 2): `data-tip*` attributes, never a `title` attribute. The card
    // shows the label in bold and the hint line, nothing else.
    function tip(attrs, key, label, hint) {
        attrs["data-tip"] = key;
        attrs["data-tip-label"] = text(label);
        attrs["data-tip-hint"] = text(hint);
        return attrs;
    }

    // A node's own tooltip: from its `hint`; `force` draws a card from the label alone (tiles, icon buttons).
    function tipAttrs(node, attrs, key, force) {
        if (!node.hint && !force) {
            return attrs;
        }
        return tip(attrs, key, node.label, node.hint);
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

    function faceNode(node, busy) {
        return h("span", "face", {"class": "deck-face"}, null, [
            node.icon && !busy ? icon(node.icon, "main", "main") : null,
            busy ? h("span", "spin", {"class": "deck-spinner"}, null, "") : null,
            icon("check", "ok", "ok"),
            icon("cross", "no", "no"),
            icon("warn", "warn", "warn")
        ]);
    }

    // The current choice of a menu: its option ({value, label, swatch}) or null.
    function menuChoice(menu) {
        var options = (menu && menu.options) || [];
        for (var i = 0; i < options.length; i++) {
            if (String(options[i].value) === String(menu.value)) {
                return options[i];
            }
        }
        return null;
    }

    function pinnedIds(tree, local) {
        if (local && local.pinsPending) {
            return local.pinsPending;
        }
        var items = tree && tree.pins && tree.pins.items;
        return items && items.length ? items : [];
    }

    function pinsMax(tree) {
        var max = tree && tree.pins ? Number(tree.pins.max) : 0;
        return max > 0 ? Math.floor(max) : PINS_MAX;
    }

    function isPinned(ctx, id) {
        var ids = pinnedIds(ctx.tree, ctx.local);
        for (var i = 0; i < ids.length; i++) {
            if (ids[i] === id) {
                return true;
            }
        }
        return false;
    }

    /*
     * A tile (PROTOCOL 6.1 `button` or `switch` inside a `tiles` group, or a pinned action): a wrapper that holds
     * the body (the press target: icon and code) and the separate targets drawn on it: the lamp of a switch, the
     * corner mark of a `menu` or a `prompt`, the AUTO capsule of `auto`, and in edit mode the cross that unpins a
     * pinned tile. `zone` is "lane" (icon over code), "row" (icon beside code, the Files tiles) or "pins".
     */
    function tileNode(node, ctx, key, zone) {
        var isSwitch = node.kind === "switch";
        var disabled = !!node.disabled || ctx.offline;
        var pending = ctx.local.pending || {};
        var on = isSwitch && (pending.hasOwnProperty(node.id) ? !!pending[node.id] : !!node.value);
        var flash = ctx.local.flash ? ctx.local.flash[node.id] : null;
        var label = text(node.label);
        var code = node.short ? String(node.short) : label;
        var editing = !!ctx.local.editing && ctx.pinsHere;
        var body = {"class": cls("deck-t-body", disabled && "is-disabled"), "data-action": node.id, "data-kind": isSwitch ? "switch" : "button",
            "data-zone": zone, "aria-label": label};
        if (isSwitch) {
            body.role = "switch";
            body["aria-checked"] = on ? "true" : "false";
            body["data-value"] = on ? "true" : "false";
        }
        // A tile with `confirm` arms itself on a press (the face flips into the check and the cross); a pinned copy
        // arms on its own (confirmation key with the "pins|" prefix).
        var confirmKey = (zone === "pins" ? "pins|" : "") + node.id;
        var armed = !!node.confirm && ctx.local.confirming === confirmKey && !disabled && !node.busy && !ctx.local.editing;
        if (node.confirm) {
            body["data-confirm"] = confirmKey;
        }
        tip(body, key, label, node.hint);
        var parts = [h("button", "body", body, {disabled: disabled}, [
            faceNode(node, !!node.busy),
            h("span", "code", {"class": "deck-t-code"}, null, code)
        ])];
        if (isSwitch) {
            parts.push(h("span", "lamp", {"class": "deck-lamp"}, null, ""));
        }
        var corner = null;
        if (node.menu && node.menu.id) {
            var choice = menuChoice(node.menu);
            var current = choice ? text(choice.label) : text(node.menu.badge);
            corner = tip({"class": "deck-corner", "data-menu-open": node.id, "aria-label": label + (current ? ": " + current : "")},
                key + "|corner", label, current ? "Current: " + current + "." : "");
            if (choice && SWATCH.test(text(choice.swatch))) {
                corner.style = "--mark: " + choice.swatch;
            }
        } else if (node.prompt && node.prompt.id) {
            var value = text(node.prompt.value);
            corner = tip({"class": "deck-corner", "data-prompt-open": node.id, "aria-label": text(node.prompt.label || label)},
                key + "|corner", node.prompt.label || label, value ? "Current: " + value + "." : "");
        }
        if (corner) {
            parts.push(h("button", "corner", corner, {disabled: disabled}, ""));
        }
        var auto = node.auto && node.auto.id ? node.auto : null;
        var autoOn = false;
        if (auto) {
            autoOn = pending.hasOwnProperty(auto.id) ? !!pending[auto.id] : !!auto.value;
            parts.push(h("button", "auto", tip({"class": cls("deck-auto", autoOn && "is-on"), "data-action": auto.id,
                "data-kind": "auto", "data-value": autoOn ? "true" : "false", "data-zone": zone, role: "switch",
                "aria-checked": autoOn ? "true" : "false", "aria-label": text(auto.label)}, key + "|auto", auto.label, auto.hint),
                {disabled: ctx.offline}, [
                    h("span", "word", {"class": "deck-auto-word"}, null, "AUTO"),
                    h("span", "cap", {"class": "deck-capsule"}, null, [h("i", "knob", {"class": "deck-capsule-knob"}, null, "")])
                ]));
        }
        if (node.confirm) {
            // The flipped face: always in the tree (so it can flip back), a check on the left, a cross on the right and,
            // on a wide row tile, the question between them (the display hides the question when it does not fit).
            var question = String(node.confirm);
            parts.push(h("div", "armed", {"class": "deck-armed", "data-armed": "1", "aria-hidden": armed ? "false" : "true"}, null, [
                h("button", "yes", {"class": "deck-armed-yes", "data-confirm-yes": confirmKey, "data-action": node.id,
                    "aria-label": "Confirm: " + question}, {disabled: !armed}, [icon("check", "", "icon")]),
                zone === "row" ? h("span", "q", {"class": "deck-armed-q"}, null, question) : null,
                h("button", "no", {"class": "deck-armed-no", "data-confirm-no": confirmKey, "aria-label": "Cancel"},
                    {disabled: !armed}, [icon("cross", "", "icon")])
            ]));
        }
        if (zone === "pins" && editing) {
            parts.push(h("button", "unpin", {"class": "deck-unpin", "data-unpin": node.id, "aria-label": "Unpin " + label},
                {}, [icon("cross", "", "icon")]));
        }
        var wrapAttrs = {"class": cls("deck-node deck-t", "is-" + zone, isSwitch && "is-switch", on && "is-on",
            auto && "has-auto", node.busy && "is-busy", disabled && "is-off", armed && "is-armed",
            node.tone && node.tone !== "neutral" && tone(node.tone), flash && FLASH_CLASS[flash],
            editing && zone === "lane" && "is-pinnable", editing && zone === "lane" && isPinned(ctx, node.id) && "is-pinned"),
            "data-tile": node.id, "data-zone": zone, "data-anchor": zone + ":" + node.id};
        if (armed) {
            // While armed the tile's tooltip is the question itself, shown at once and kept until the tile disarms.
            tip(wrapAttrs, key + "|armed", question, "");
            wrapAttrs["data-tip-armed"] = "1";
        }
        return h("div", key, wrapAttrs, null, parts);
    }

    /*
     * A `button` node as a list of vnodes: the button itself and, while its inline question is open, the
     * confirmation as a sibling (a tile arms itself instead; in a column grid it spans the full
     * row under the asking button's row; in a list row it stays inside the row). Forms: a tile inside a `tiles` group, a round icon button (`variant` "icon"), a plain
     * pill. The vnode of a button carries `btn: true` (grid runs) and the confirmation `ask: true`.
     */
    function buttonNodes(node, ctx, rowId, key, zone) {
        var disabled = !!node.disabled || ctx.offline;
        var confirmKey = node.id + (rowId ? "|" + rowId : "");
        var isIcon = node.variant === "icon";
        var label = text(node.label);
        var attrs = {"data-action": node.id, "data-kind": "button"};
        var vnode;
        if (rowId) {
            attrs["data-row"] = rowId;
        }
        if (node.confirm) {
            attrs["data-confirm"] = confirmKey;
        }
        if (zone && !isIcon) {
            vnode = tileNode(node, ctx, key, zone);
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
        // A tile arms itself (tileNode); the inline question stays for the other buttons (list rows, column grids).
        var confirming = !zone && !!node.confirm && ctx.local.confirming === confirmKey && !disabled && !node.busy;
        if (confirming) {
            var ask = confirmNode(node, confirmKey, rowId, key + "|ask");
            ask.ask = true;
            out.push(ask);
        }
        return out;
    }

    function toggleNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        return h("label", key, tipAttrs(node, {"class": cls("deck-node deck-toggle", disabled && "is-disabled")}, key, false), null, [
            h("input", "input", {type: "checkbox", "class": "deck-toggle-input", "data-action": node.id, "data-kind": "toggle"},
                {checked: !!node.value, disabled: disabled}),
            h("span", "track", {"class": "deck-toggle-track"}, null, ""),
            h("span", "label", {"class": "deck-toggle-label"}, null, text(node.label))
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
            h("span", "label", {"class": "deck-field-label"}, null, text(node.label)),
            h("select", "select", {"class": "deck-select", "data-action": node.id, "data-kind": "select"},
                {value: text(node.value), disabled: disabled}, options)
        ]);
    }

    function inputNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        var row = [
            h("input", "input", {type: "text", spellcheck: "false", autocomplete: "off", "class": "deck-input", "data-action": node.id, "data-kind": "input",
                placeholder: text(node.placeholder)}, {value: text(node.value), disabled: disabled})
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
        var value = text(node.text);
        var attrs;
        if (node.chip) {
            attrs = tipAttrs(node, {"class": cls("deck-node deck-chip", tone(node.tone))}, key, false);
            return h("span", key, attrs, null, [
                h("i", "dot", {"class": "deck-chip-dot"}, null, ""),
                h("span", "text", {"class": "deck-chip-text"}, null, value)
            ]);
        }
        attrs = tipAttrs(node, {"class": cls("deck-node deck-text", tone(node.tone), node.small && "is-small",
            PATH_TEXT.test(value) && "is-path")}, key, false);
        return h("div", key, attrs, null, value);
    }

    function progressNode(node, ctx, key) {
        var indeterminate = node.value === null || node.value === undefined;
        var pct = indeterminate ? 100 : Math.max(0, Math.min(1, Number(node.value))) * 100;
        var disabled = ctx.offline;
        return h("div", key, tipAttrs(node, {"class": cls("deck-node deck-progress", indeterminate && "is-indeterminate")}, key, false), null, [
            h("div", "top", {"class": "deck-progress-top"}, null, [
                h("span", "label", {"class": "deck-progress-label"}, null, text(node.label)),
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

    /*
     * A `swatches` node (PROTOCOL 6.1, 3.2): one colour cell per option, `columns` cells per row; the current value
     * carries a ring (a pressed cell shows it at once from `local.pending`). A click sends `action {id, value}` like a
     * `select`. Each cell's tooltip is the option's label over the node's hint. Inside a lane the cells sit on the
     * lane's plate under its tiles; elsewhere they take the full width.
     */
    function swatchesNode(node, ctx, key) {
        var disabled = !!node.disabled || ctx.offline;
        var pending = ctx.local.pending || {};
        var current = pending.hasOwnProperty(node.id) ? pending[node.id] : node.value;
        var columns = Math.max(1, Math.min(16, Math.floor(Number(node.columns) || 8)));
        var cells = [];
        var options = node.options || [];
        for (var i = 0; i < options.length; i++) {
            var option = options[i] || {};
            var colour = text(option.swatch);
            if (!SWATCH.test(colour)) {
                continue;   // the display draws only #rgb / #rrggbb values
            }
            var value = text(option.value);
            var on = current !== null && current !== undefined && String(current) === value;
            var attrs = {"class": cls("deck-swatch", on && "is-on"), "data-swatch": node.id, "data-value": value,
                "aria-label": text(option.label), "aria-pressed": on ? "true" : "false", style: "--sw: " + colour};
            tip(attrs, key + "|" + value, option.label, node.hint);
            cells.push(h("button", "s:" + value, attrs, {disabled: disabled}, ""));
        }
        return h("div", key, {"class": cls("deck-node deck-swatches", disabled && "is-disabled"), role: "group",
            "aria-label": text(node.label), style: "--sw-cols: " + columns}, null, cells);
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
            children.push(h("div", "empty", {"class": "deck-list-empty"}, null, text(node.empty)));
        }
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            var actions = [];
            var buttons = row.actions || [];
            for (var j = 0; j < buttons.length; j++) {
                actions = actions.concat(buttonNodes(buttons[j], ctx, row.id, "a:" + buttons[j].id, null));
            }
            // A row whose button asks for confirmation wraps, so the question gets the full row width.
            var asking = !!ctx.local.confirming && String(ctx.local.confirming).split("|")[1] === String(row.id);
            children.push(h("div", "r:" + row.id, {"class": cls("deck-list-row", tone(row.tone), asking && "is-confirming")}, null, [
                row.thumb ? h("img", "thumb", {"class": "deck-list-thumb", src: String(row.thumb), alt: ""}, null, "") : null,
                h("div", "text", {"class": "deck-list-text"}, null, [
                    h("div", "title", {"class": "deck-list-title"}, null, text(row.title)),
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
        return "t:" + text(node.title);
    }

    // The code at a lane's left edge: the group's `short`, else the first four letters of its title.
    function laneCode(node) {
        if (node.short) {
            return String(node.short);
        }
        return text(node.title).replace(/\s+/g, "").substring(0, 4).toUpperCase();
    }

    // The result line of a group or of a section (6.6): outcome icon, the text, the time, a chevron with a detail.
    // Every line is a notice that closes on a click (CONVENTIONS "Notices"); a line with a detail opens the
    // detail with the same click. The open detail stays until its own click, Escape or the group's next result.
    function resultLine(line, lineKey, prefix) {
        var hasDetail = !!line.detail;
        var inner = [
            icon(OUTCOME_ICON[line.outcome] || "info", "", "icon"),
            prefix ? h("span", "code", {"class": "deck-g-result-code"}, null, prefix + " ·") : null,
            h("span", "text", {"class": "deck-g-result-text"}, null, text(line.text)),
            h("time", "time", {"class": "deck-g-result-time"}, null, clock(line.at)),
            hasDetail ? icon("chevron", "more", "more") : null
        ];
        return h("button", "line", {"class": cls("deck-g-result", tone(line.outcome), hasDetail && "has-detail"),
            "data-line": lineKey}, {}, inner);
    }

    // The explanation a failed line opened: one line, closed by a click on it.
    function detailLine(value, lineKey) {
        return h("button", "why", {"class": "deck-g-why", "data-why": lineKey}, {}, String(value));
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

    // Tiles and the other nodes: a lane or a block of row tiles holds its tiles in one fixed grid, any other node of
    // the group follows at full width. Only a lane (`inLane`) takes swatches onto its plate and chips into its tile
    // row; elsewhere they follow in order like any other node.
    function splitTiles(items, inLane) {
        var tiles = [];
        var rest = [];
        var plate = [];
        var aside = [];
        for (var i = 0; i < items.length; i++) {
            if (items[i].btn) {
                tiles.push(items[i]);
            } else if (inLane && items[i].plate) {
                plate.push(items[i]);
            } else if (inLane && items[i].aside) {
                aside.push(items[i]);
            } else {
                rest.push(items[i]);
            }
        }
        return {tiles: tiles, rest: rest, plate: plate, aside: aside};
    }

    // The tile groups of a section (lanes and blocks of row tiles, also inside a row): their result lines go to the tab's notice slot. A lane's prefix is its code; a block without a title has none.
    function slotGroups(section) {
        var out = [];
        var visit = function (nodes) {
            for (var i = 0; i < (nodes || []).length; i++) {
                var node = nodes[i];
                if (node && node.kind === "group" && node.tiles) {
                    out.push({key: groupId(node), prefix: text(node.title) ? laneCode(node) : ""});
                } else if (node && node.kind === "row") {
                    visit(node.items);
                }
            }
        };
        visit(section && section.items);
        return out;
    }

    /*
     * The notice slot of a tab (docs/PANEL.md 2): one line of a fixed height directly under the tab's content (the
     * last lane; on Files the last group), inside the tab panel, so it stays beside the tiles in a tall panel. It is
     * always in the tree while the tab has lanes, tile blocks or the pinned row, so a result line never moves a
     * tile. It shows results only (a confirmation arms the tile itself): the newest result line of the tab's lanes,
     * pinned row and tile blocks (prefixed with the lane's code, "PIN" for the pinned row, no prefix for a block of
     * row tiles), else nothing. With a line the slot is the tooltip target (`data-tip-clip`: the card shows only
     * when the text is cut by the slot's width). A detail the first click on a line opened is a small card above
     * the slot.
     */
    function noticeSlot(section, ctx) {
        var sources = slotGroups(section);
        if (ctx.pinsHere) {
            sources.unshift({key: PINS_LINE, prefix: "PIN"});
        }
        if (!sources.length) {
            return null;
        }
        var lines = ctx.local.lines || {};
        var open = ctx.local.detailOpen || {};
        var best = null;
        var why = null;
        for (var i = 0; i < sources.length; i++) {
            var line = lines[sources[i].key];
            if (line && (!best || Number(line.seq || 0) >= Number(best.line.seq || 0))) {
                best = {line: line, key: sources[i].key, prefix: sources[i].prefix};
            }
            if (open[sources[i].key]) {
                why = {key: sources[i].key, value: open[sources[i].key]};
            }
        }
        var attrs = {"class": "deck-notice-slot", "data-slot": section.id};
        var content = [];
        if (best) {
            content.push(resultLine(best.line, best.key, best.prefix));
            tip(attrs, "slot", (best.prefix ? best.prefix + " · " : "") + text(best.line.text),
                best.line.detail ? "Click to read more." : "");
            attrs["data-tip-clip"] = "1";
        }
        attrs["data-filled"] = content.length ? "1" : "0";
        return h("div", "notice", {"class": "deck-notice-box"}, null, [
            why ? h("button", "why", {"class": "deck-g-why is-card", "data-why": why.key}, {}, String(why.value)) : null,
            h("div", "slot", attrs, null, content)
        ]);
    }

    /*
     * A `group` (PROTOCOL 6.1). With `tiles` and a title it is a lane: one row with the group's code at the left
     * edge (the full title in its tooltip) and fixed 46 x 44 tiles (icon over code, never stretched). With `tiles`
     * and no title it is a block of row tiles (icon beside code, two per row). Otherwise a caption over its items;
     * with columns 2 or 3 every run of consecutive buttons becomes a grid of that many columns, and with
     * `collapsible` the caption is the collapse control. Under the items the group keeps its result line.
     */
    function groupNode(node, ctx, key) {
        var gid = groupId(node);
        var title = text(node.title);
        if (node.tiles) {
            var zone = title ? "lane" : "row";
            var parts = splitTiles(renderNodes(node.items, ctx, zone), zone === "lane");
            var cells = parts.tiles.slice();
            if (parts.aside.length) {
                // The chips of a lane (the Labels target) share its last tile row: they take the cells the tiles leave.
                var free = LANE_COLS - (parts.tiles.length % LANE_COLS);
                cells.push(h("div", "aside", {"class": "deck-lane-aside", style: "grid-column: span " + free}, null, parts.aside));
            }
            var grid = h("div", "tiles", {"class": zone === "lane" ? "deck-lane-tiles" : "deck-rowtiles"}, null, cells);
            if (zone === "lane") {
                // Swatches sit on the plate of the lane under its tiles (PANEL.md 2); a lane without them keeps one row.
                var lane = parts.plate.length ? h("div", "body", {"class": "deck-lane-body"}, null, [grid].concat(parts.plate)) : grid;
                return h("section", key, {"class": "deck-node deck-group deck-lane-wrap", "data-lane": gid}, null, [
                    h("div", "lane", {"class": "deck-lane"}, null, [
                        h("span", "code", tip({"class": "deck-lane-code"}, "lane:" + gid, title, ""), null, laneCode(node)),
                        lane
                    ])
                ].concat(parts.rest));
            }
            return h("section", key, {"class": "deck-node deck-group is-rowtiles"}, null, [grid].concat(parts.rest));
        }
        var columns = Math.max(1, Math.min(3, Number(node.columns) || 1));
        var collapsible = !!node.collapsible;
        var collapsed = collapsible && !!(ctx.local.collapsed && ctx.local.collapsed[gid]);
        var items = renderNodes(node.items, ctx, null);
        var count = 0;
        var i;
        // The count a collapsed caption shows: its buttons and switches, and the rows of its lists.
        for (i = 0; i < (node.items || []).length; i++) {
            var item = node.items[i] || {};
            if (item.kind === "button" || item.kind === "switch") {
                count++;
            } else if (item.kind === "list") {
                count += (item.rows || []).length;
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
                body.push(h("div", "grid:" + run[0].k, {"class": "deck-grid", "data-cols": String(buttons < 2 ? 1 : columns)}, null, run));
                run = [];
            }
        };
        for (i = 0; i < items.length; i++) {
            if (columns >= 2 && (items[i].btn || (items[i].ask && run.length))) {
                run.push(items[i]);
            } else {
                flush();
                body.push(items[i]);
            }
        }
        flush();
        body = body.concat(resultNodes(ctx, gid));
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
        return h("section", key, {"class": cls("deck-node deck-group", collapsed && "is-collapsed")}, null, [
            head,
            collapsed ? null : h("div", "items", {"class": "deck-g-body"}, null, body)
        ]);
    }

    // `zone`: the tile form of the buttons and switches of these nodes ("lane", "row") or null outside a tile group.
    function renderNodes(nodes, ctx, zone) {
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
                out = out.concat(buttonNodes(node, ctx, null, key, zone));
                break;
            case "switch":
                // Outside a tile group a switch is a row tile of the full width.
                vnode = tileNode(node, ctx, key, zone || "row");
                vnode.btn = true;
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
                vnode.aside = !!node.chip;
                break;
            case "swatches":
                vnode = swatchesNode(node, ctx, key);
                vnode.plate = true;
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
        var hasIconButton = false;
        for (var i = 0; i < items.length; i++) {
            if (items[i] && items[i].kind === "button" && items[i].variant === "icon") {
                hasIconButton = true;
            }
        }
        var head = hasIconButton && items.length > 1 && items[0] && items[0].kind === "text" && !items[0].chip;
        var chips = !!(items[0] && items[0].kind === "text" && items[0].chip);
        return h("div", key, {"class": cls("deck-node deck-row", head && "is-head", chips && "is-chips")}, null,
            renderNodes(items, ctx, null));
    }

    // --- the pinned row -------------------------------------------------------------------------------------------

    // Whether a section holds a lane (a titled tile group): the pinned row is drawn on the first such section.
    function hasLane(section) {
        var items = (section && section.items) || [];
        for (var i = 0; i < items.length; i++) {
            if (items[i] && items[i].kind === "group" && items[i].tiles && items[i].title) {
                return true;
            }
        }
        return false;
    }

    function pinsSectionId(tree) {
        var sections = (tree && tree.sections) || [];
        for (var i = 0; i < sections.length; i++) {
            if (hasLane(sections[i])) {
                return sections[i].id;
            }
        }
        return null;
    }

    /*
     * The pinned row (tree `pins`, PROTOCOL 6.1): the referenced buttons and switches as tiles (an id with no node
     * is skipped), an empty slot per free place up to `max`, and the edit slot: "+" opens edit mode, the check ends
     * it. In edit mode every pinned tile carries a cross that unpins it.
     */
    function pinsNode(ctx) {
        var ids = pinnedIds(ctx.tree, ctx.local);
        var max = pinsMax(ctx.tree);
        var editing = !!ctx.local.editing;
        var tiles = [];
        for (var i = 0; i < ids.length; i++) {
            var found = findButton(ctx.tree, ids[i]);
            if (found && tiles.length < max) {
                tiles.push(tileNode(found.node, ctx, "pin:" + found.node.id, "pins"));
            }
        }
        for (var s = tiles.length; s < max; s++) {
            tiles.push(h("span", "slot:" + s, {"class": "deck-slot"}, null, ""));
        }
        tiles.push(h("button", "edit", tip({"class": cls("deck-slot is-edit", editing && "is-on"), "data-pins-edit": "1",
            "aria-pressed": editing ? "true" : "false", "aria-label": editing ? "Done" : "Edit pinned"}, "pins:edit",
            editing ? "Done" : "Edit pinned", editing ? "Ends editing." : "Press tiles below to pin or unpin them; up to " + max + "."),
            {disabled: ctx.offline}, [icon(editing ? "check" : "plus", "", "icon")]));
        return h("section", "pins", {"class": "deck-node deck-group deck-lane-wrap is-pins", "data-lane": PINS_LINE}, null, [
            h("div", "lane", {"class": "deck-lane is-pinned"}, null, [
                h("span", "code", tip({"class": "deck-lane-code"}, "lane:" + PINS_LINE, "Pinned", ""), null, [icon("pin", "", "icon")]),
                h("div", "tiles", {"class": "deck-lane-tiles"}, null, tiles)
            ])
        ]);
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
                h("span", "label", {}, null, text(section.title))
            ]));
        }
        return h("div", "tabs", {"class": "deck-tabs", role: "tablist", style: "--n: " + sections.length + "; --i: " + Math.max(0, index),
            "data-on": index >= 0 ? sections[index].id : ""}, null, tabs);
    }

    // A row of buttons that is followed by nothing but groups docks to the foot of its tab (the Files undo row).
    function dockIndex(items) {
        for (var i = items.length - 1; i >= 0; i--) {
            var item = items[i];
            if (!item) {
                continue;
            }
            if (item.kind === "group") {
                continue;
            }
            if (item.kind !== "row" || !(item.items || []).length) {
                return -1;
            }
            for (var j = 0; j < item.items.length; j++) {
                if (!item.items[j] || item.items[j].kind !== "button") {
                    return -1;
                }
            }
            return i;
        }
        return -1;
    }

    // The selected tab's content: the pinned row (on the first section with lanes), its nodes, the section-level
    // result line, the docked row. A section's `status` is accepted and not drawn.
    function tabPanel(section, ctx) {
        var items = section.items || [];
        var dockAt = dockIndex(items);
        var nodes = renderNodes(items, ctx, null);
        var dock = null;
        if (dockAt >= 0) {
            var dockKey = items[dockAt].id ? "n:" + items[dockAt].id : "p:" + dockAt + ":row";
            for (var i = 0; i < nodes.length; i++) {
                if (nodes[i].k === dockKey) {
                    dock = nodes.splice(i, 1)[0];
                    dock.a["class"] += " is-dock";
                    break;
                }
            }
        }
        return h("div", "tab:" + section.id, {"class": cls("deck-tabpanel", ctx.pinsHere && ctx.local.editing && "is-editing")}, null,
            [ctx.pinsHere ? pinsNode(ctx) : null].concat(nodes, resultNodes(ctx, "~" + section.id), [noticeSlot(section, ctx)],
                dock ? [dock] : []));
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
                SWATCH.test(text(options[i].swatch)) ? h("i", "sw", {"class": "deck-menu-swatch",
                    style: "background-color: " + options[i].swatch}, null, "") : null,
                h("span", "label", {}, null, String(options[i].label))
            ]));
        }
        return h("div", "menu", {"class": cls("deck-menu", menu.ready && "is-ready", menu.above && "is-above",
            options.length > 8 && "is-two"), style: menu.style || "left: 0px; top: 0px", role: "menu"}, null, items);
    }

    // The popover of a `button.prompt`: one text field and its submit button (Enter submits too).
    function promptNode(local, tree) {
        var state = local.prompt;
        if (!state) {
            return null;
        }
        var button = findButton(tree, state.buttonId);
        var spec = button && button.node.prompt;
        if (!spec || !spec.id) {
            return null;
        }
        return h("div", "prompt", {"class": cls("deck-pop", state.ready && "is-ready", state.above && "is-above"),
            style: state.style || "left: 0px; top: 0px", role: "dialog", "aria-label": text(spec.label)}, null, [
            h("input", "field", {type: "text", spellcheck: "false", autocomplete: "off", "class": "deck-input deck-pop-field", "data-prompt-field": spec.id,
                placeholder: text(spec.placeholder), "aria-label": text(spec.label)}, {value: text(spec.value)}),
            h("button", "go", {"class": "deck-btn tone-accent is-small", "data-prompt-submit": spec.id}, {}, "Apply")
        ]);
    }

    function tipNode(local) {
        var card = local.tip;
        if (!card) {
            return null;
        }
        return h("div", "tip", {"class": cls("deck-tip", card.ready && "is-ready", card.below && "is-below"), role: "tooltip",
            style: card.style || "left: 0px; top: 0px"}, null, [
            card.label ? h("b", "label", {}, null, card.label) : null,
            card.hint ? h("span", "hint", {}, null, card.hint) : null,
            h("i", "arrow", {"class": "deck-tip-arrow", style: "left: " + (card.arrow || 24) + "px"}, null, "")
        ]);
    }

    // The footer's version: "v" and the version number of the footer text, else the text as it is.
    function footerVersion(value) {
        var found = VERSION_TEXT.exec(text(value));
        return found ? "v" + found[1] : text(value);
    }

    function clampScale(value) {
        var n = Math.round((Number(value) || SCALE_DEFAULT) / SCALE_STEP) * SCALE_STEP;
        return Math.min(SCALE_MAX, Math.max(SCALE_MIN, n));
    }

    // The scale at start: the stored one, else 125 % on a screen scaled 125 % or more, else 100 %.
    function initialScale(stored, pixelRatio) {
        if (stored !== null && stored !== undefined && stored !== "" && !isNaN(Number(stored))) {
            return clampScale(stored);
        }
        return Number(pixelRatio) >= SCALE_HIDPI / 100 ? SCALE_HIDPI : SCALE_DEFAULT;
    }

    function scaleNode(scale) {
        var pct = ((scale - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)) * 100;
        return h("label", "scale", tip({"class": "deck-scale"}, "scale", "Panel scale", "Scales the whole panel; 125 % suits a 2K monitor."), null, [
            icon("scale", "", "icon"),
            h("span", "track", {"class": "deck-scale-track"}, null, [
                h("input", "input", {type: "range", "class": "deck-scale-input", min: String(SCALE_MIN), max: String(SCALE_MAX),
                    step: String(SCALE_STEP), "data-scale": "1", "aria-label": "Panel scale", style: "--p: " + pct.toFixed(2) + "%"},
                    {value: String(scale)})
            ]),
            h("output", "value", {"class": "deck-scale-value"}, null, scale + " %")
        ]);
    }

    function footerNode(tree, local, connected) {
        var footer = tree && tree.footer;
        var version = footer ? footerVersion(footer.text) : (local.panelVersion ? "v" + local.panelVersion : "");
        var link = footer && footer.link && footer.link.action ? footer.link : {label: "Open host", action: ""};
        return h("footer", "footer", {"class": "deck-footer"}, null, [
            h("span", "text", {"class": "deck-ver"}, null, version),
            scaleNode(clampScale(local.scale)),
            h("button", "link", {"class": cls("deck-small-btn deck-link", !connected && "is-disabled"), "data-action": link.action || "host.open",
                "data-kind": "link"}, {disabled: !connected}, [
                icon("open", "", "icon"),
                h("span", "label", {}, null, text(link.label || "Open host"))
            ])
        ]);
    }

    // The stage's zoom and height: 640 CSS px of the stage, or the whole panel height when that is taller.
    function stageStyle(local) {
        var zoom = clampScale(local.scale) / 100;
        var view = Number(local.viewHeight) || STAGE_H;
        var height = Math.max(STAGE_H, Math.floor(view / zoom));
        return "zoom: " + zoom + "; min-height: " + height + "px";
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
     *   lines        {groupId | "~" + sectionId | "~pins": {outcome, text, detail, at}} the result lines (3 s / 10 s)
     *   detailOpen   {line key: detail text} the explanations a click on a failed line opened
     *   pending      {switch or auto id: bool} the value a clicked switch or capsule shows until the host's next tree
     *   pinsPending  [action ids] the pinned row a pin or unpin shows until the host's next tree, or null
     *   editing      the pinned row is in edit mode
     *   scale        the stage scale in per cent (80..150), remembered in localStorage
     *   viewHeight   the panel's height in CSS px (the stage grows to it)
     *   hintClosed   the connecting screen's "Start Premiere Deck on this computer" line was clicked away
     *   restartClosed the restart notice was clicked away
     *   confirming   key of the button whose confirmation is open, or null
     *   menu         {buttonId, style, ready, above} the open tile menu, or null
     *   prompt       {buttonId, style, ready, above} the open prompt popover, or null
     *   tip          {key, label, hint, style, arrow, ready, below} the open tooltip, or null
     *   toast        {text, tone} or null
     *   hostVersion  host version for the protocol notice
     *   panelVersion the panel's own version for the footer while no tree is there
     */
    function renderTree(tree, local) {
        local = local || {};
        var connected = local.connection === "connected" || local.connection === "restart_pending";
        var screen = !tree || local.connection === "protocol";
        var sections = (tree && tree.sections) || [];
        var stage = [];
        if (screen) {
            stage.push(h("main", "body", {"class": "deck-body is-screen"}, null, [connectingScreen(local)]));
            stage.push(footerNode(null, local, false));
        } else {
            var index = selectedTab(sections, local);
            var dot = tree.status ? text(tree.status.text) : "";
            var ctx = {local: local, offline: !connected, tree: tree,
                pinsHere: index >= 0 && sections[index].id === pinsSectionId(tree)};
            stage.push(h("header", "header", {"class": "deck-top"}, null, [
                glyph(24),
                sections.length ? tabsNode(sections, index) : h("div", "title", {"class": "deck-title"}, null, text((tree && tree.title) || "Premiere Deck")),
                h("span", "conn", tip({"class": cls("deck-conn", tone(connected ? "ok" : "warn")), role: "img",
                    "aria-label": connected ? "Connected" : "Connecting to Host"}, "conn", connected ? "Connected" : "Connecting to Host", dot), null, [
                    h("i", "dot", {"class": "deck-conn-dot"}, null, "")
                ])
            ]));
            if (local.connection === "restart_pending" && !local.restartClosed) {
                stage.push(h("div", "notice", {"class": "deck-notice tone-warn", "data-dismiss": "restart"}, null,
                    "Restart Premiere Pro to finish the Premiere Deck update"));
            }
            stage.push(h("main", "body", {"class": cls("deck-body", !connected && "is-offline")}, null,
                index >= 0 ? [tabPanel(sections[index], ctx)] : []));
            stage.push(footerNode(tree, local, connected));
        }
        if (local.toast) {
            stage.push(h("div", "toast", {"class": cls("deck-toast", tone(local.toast.tone)), "data-dismiss": "toast"}, null,
                text(local.toast.text)));
        }
        stage.push(menuNode(local, tree));
        stage.push(promptNode(local, tree));
        stage.push(tipNode(local));
        return h("div", "root", {"class": "deck"}, null, [
            h("div", "scene", {"class": "deck-scene", "aria-hidden": "true"}, null, [
                h("i", "f1", {"class": "f1"}, null, ""), h("i", "f2", {"class": "f2"}, null, ""), h("i", "f3", {"class": "f3"}, null, "")
            ]),
            h("div", "stage", {"class": "deck-stage", style: stageStyle(local)}, null, stage)
        ]);
    }

    // --- tree lookups, results ---------------------------------------------------------------------------------

    // The button or switch node `id` with the section and group that hold it: {node, section, group} or null. A
    // button in a list row is found as well (its group is null), and so is the button that carries the menu,
    // the prompt or the automation capsule with that id.
    function findButton(tree, id) {
        var sections = (tree && tree.sections) || [];
        var found = null;
        var carries = function (node) {
            return node.id === id || (node.menu && node.menu.id === id) || (node.prompt && node.prompt.id === id) ||
                (node.auto && node.auto.id === id);
        };
        var visit = function (nodes, section, group) {
            for (var i = 0; i < (nodes || []).length && !found; i++) {
                var node = nodes[i];
                if (!node) {
                    continue;
                }
                if ((node.kind === "button" || node.kind === "switch") && carries(node)) {
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

    // The key of the result line a result for button `id` belongs to: the pinned row's when the press came from
    // a pinned tile (`zone` "pins"), else the group's id, else the section's; null when the button is not in the
    // tree.
    function lineKeyFor(tree, id, zone) {
        var found = findButton(tree, id);
        if (!found) {
            return null;
        }
        if (zone === "pins") {
            return PINS_LINE;
        }
        return found.group ? groupId(found.group) : "~" + found.section.id;
    }

    // Every line key the tree can hold: its groups, its sections and the pinned row.
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
        if (pinsSectionId(tree) !== null) {
            keys[PINS_LINE] = true;
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
     * Where a floating card (tooltip, menu, popover) goes. `anchor` {left, top, width, height} is the element,
     * `size` {w, h} the card, `bounds` {w, h} the stage, all in stage pixels. The card is centred on the element
     * (`align` "center", the tooltip) or hangs from its right edge (`align` "end", the menu and the popover), and
     * stays inside the 12 px gutter. A tooltip goes above the element and flips below when there is no room above;
     * a menu opens downwards when it fits. `arrow` is the nub's x inside the card.
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
     * An element's rectangle in stage pixels. `stageRect` is the stage on screen; `k` = its width on screen / 360,
     * so the result holds whether the engine reports zoomed rectangles or not.
     */
    function stageRect(rect, stage) {
        var k = stage.width / STAGE_W || 1;
        return {left: (rect.left - stage.left) / k, top: (rect.top - stage.top) / k, width: rect.width / k, height: rect.height / k};
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
            pinsPending: null,
            editing: false,
            scale: initialScale(storageGet(this.win, KEY_SCALE), this.win.devicePixelRatio),
            viewHeight: Number(this.win.innerHeight) || STAGE_H,
            hintClosed: false,
            restartClosed: false,
            confirming: null,
            menu: null,
            prompt: null,
            tip: null,
            toast: null,
            hostVersion: "",
            panelVersion: ""
        };
        this.zones = {};            // action id -> the zone of its last press ("pins" sends its result to that row)
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
        this.fitQuestions();
        this.checkFloating();
    };

    // The question drawn between the check and the cross of a wide tile is shown only when it fits on one line (the
    // tooltip carries it either way).
    Display.prototype.fitQuestions = function () {
        var list = this.container.querySelectorAll ? this.container.querySelectorAll(".deck-armed-q") : [];
        for (var i = 0; i < list.length; i++) {
            list[i].style.visibility = "visible";
            list[i].style.visibility = Number(list[i].scrollWidth) > Number(list[i].clientWidth) + 1 ? "hidden" : "visible";
        }
    };

    // The armed tile's tooltip (the question) is open.
    Display.prototype.armedTip = function () {
        return !!(this.local.tip && this.local.confirmTile && /[|]armed$/.test(String(this.local.tip.key)));
    };

    // Opens (`key`, `tile` true: an armed tile that flips back after 4 s) or closes (`key` null) a confirmation.
    Display.prototype.setConfirm = function (key, tile) {
        var self = this;
        if (this.confirmTimer) {
            clearTimeout(this.confirmTimer);
            this.confirmTimer = null;
        }
        this.local.confirming = key || null;
        this.local.confirmTile = !!(key && tile);
        if (key && tile) {
            this.confirmTimer = setTimeout(function () {
                self.confirmTimer = null;
                if (self.local.confirming === key) {
                    self.setConfirm(null);
                    self.render();
                }
            }, CONFIRM_MS);
        }
    };

    Display.prototype.stage = function () {
        return this.container.querySelector(".deck-stage");
    };

    // A floating card whose element is gone after a re-render is closed; a card that was just opened is measured
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
        var floats = [["menu", ".deck-menu"], ["prompt", ".deck-pop"]];
        for (var i = 0; i < floats.length; i++) {
            var state = local[floats[i][0]];
            if (!state) {
                continue;
            }
            var opener = box.querySelector("[data-anchor=\"" + cssEscape(state.anchor || "lane:" + state.buttonId) + "\"]");
            var card = box.querySelector(floats[i][1]);
            if (!opener || !card) {
                local[floats[i][0]] = null;
                this.render();
                return;
            }
            if (!state.ready) {
                this.placeCard(state, opener, card, "end");
                if (floats[i][0] === "prompt") {
                    this.focusPrompt();
                }
                return;
            }
        }
    };

    Display.prototype.placeCard = function (state, target, card, align) {
        if (!card) {
            return;
        }
        var stage = this.stage();
        var frame = stage ? stage.getBoundingClientRect() : this.container.getBoundingClientRect();
        var spot = placeFloating(stageRect(target.getBoundingClientRect(), frame), {w: card.offsetWidth, h: card.offsetHeight},
            {w: STAGE_W, h: stage && stage.offsetHeight ? stage.offsetHeight : STAGE_H}, align);
        state.style = "left: " + spot.left + "px; top: " + spot.top + "px";
        state.below = spot.below;
        state.above = !spot.below;
        state.arrow = spot.arrow;
        state.ready = true;
        this.render();
    };

    function cssEscape(value) {
        return String(value).replace(/(["\\])/g, "\\$1");
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
        var attach = function (source) {
            if (!source || doc.getElementById("deck-sprite")) {
                return;
            }
            var holder = doc.createElement("div");
            holder.innerHTML = source;
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
            var source = cs && cs.readText ? cs.readText(cs.joinPath(cs.extensionDir(), "icons.svg")) : null;
            attach(source);
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
            this.setConfirm(null);
            this.local.menu = null;
            this.local.prompt = null;
            this.local.tip = null;
            this.local.editing = false;
            this.armLate();
        }
        this.render();
    };

    Display.prototype.onMessage = function (message) {
        if (message.type === "ui") {
            this.tree = message;
            this.local.pending = {};       // the host's tree is the truth for every switch and capsule
            this.local.pinsPending = null; // and for the pinned row
            this.pruneResults();
            if (this.local.confirming && !this.confirmStillValid()) {
                this.setConfirm(null);
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

    // Shows a result line under `key` with the 3 s / 10 s timer; a new line of the key replaces the old one.
    Display.prototype.showLine = function (key, outcome, value, detail, at) {
        var self = this;
        this.lineSeq = (this.lineSeq || 0) + 1;
        this.local.lines[key] = {outcome: outcome, text: value, detail: detail, at: at, seq: this.lineSeq};
        delete this.local.detailOpen[key];
        if (this.lineTimers[key]) {
            clearTimeout(this.lineTimers[key]);
        }
        this.lineTimers[key] = setTimeout(function () {
            delete self.lineTimers[key];
            delete self.local.lines[key];
            self.render();
        }, outcome === "ok" || outcome === "neutral" ? LINE_MS : LINE_ERROR_MS);
    };

    // `action_result` (PROTOCOL 6.6): the tile shows the outcome for 2 s, the group shows the line for 3 s (ok,
    // neutral) or 10 s (warn, error); a press on a pinned tile shows the line under the pinned row. A result for a
    // button that is not in the tree changes nothing.
    Display.prototype.onResult = function (message) {
        var self = this;
        var id = String(message.id || "");
        if (id === "panel.pins") {
            // The host's answer to a refused pin: the result line of the pinned row.
            var pinsOutcome = FLASH_CLASS.hasOwnProperty(message.outcome) ? message.outcome : "neutral";
            this.showLine(PINS_LINE, pinsOutcome, text(message.text), message.detail ? String(message.detail) : null, Number(message.at) || Date.now());
            this.render();
            return;
        }
        var found = findButton(this.tree, id);
        var key = lineKeyFor(this.tree, id, found ? this.zones[found.node.id] : null);
        if (key === null) {
            return;
        }
        var tileId = found.node.id;
        var outcome = FLASH_CLASS.hasOwnProperty(message.outcome) ? message.outcome : "neutral";
        this.local.flash[tileId] = outcome;
        this.showLine(key, outcome, text(message.text), message.detail ? String(message.detail) : null, Number(message.at) || Date.now());
        if (this.flashTimers[tileId]) {
            clearTimeout(this.flashTimers[tileId]);
        }
        this.flashTimers[tileId] = setTimeout(function () {
            delete self.flashTimers[tileId];
            delete self.local.flash[tileId];
            self.render();
        }, RESULT_MS);
        this.render();
    };

    // An open confirmation survives a re-render only while its button still exists and still asks.
    Display.prototype.confirmStillValid = function () {
        var key = String(this.local.confirming).replace(/^pins\|/, "");
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

    // --- scale (PANEL 3): CSS zoom on the stage, remembered per panel ---

    Display.prototype.setScale = function (value) {
        var next = clampScale(value);
        if (next === this.local.scale) {
            return;
        }
        this.local.scale = next;
        storageSet(this.win, KEY_SCALE, String(next));
        this.local.tip = null;
        this.local.menu = null;
        this.local.prompt = null;
        this.render();
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

    // --- tooltip, menu, prompt ---

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

    // Whether the text a notice slot shows is cut: a result line by the slot's width, a question by its two lines.
    function slotClipped(el) {
        var target = el.querySelector(".deck-g-result-text") || el.querySelector(".deck-confirm-text");
        return !!target && (Number(target.scrollWidth) > Number(target.clientWidth) + 1 ||
            Number(target.scrollHeight) > Number(target.clientHeight) + 1);
    }

    Display.prototype.armTip = function (el) {
        var self = this;
        if (this.tipTimer) {
            clearTimeout(this.tipTimer);
        }
        this.tipTimer = setTimeout(function () {
            self.tipTimer = null;
            if (self.local.menu || self.local.prompt || !el.parentNode) {
                return;
            }
            if (el.hasAttribute("data-tip-clip") && !slotClipped(el)) {
                return;   // a notice slot shows its card only when the line is cut
            }
            self.local.tip = {key: el.getAttribute("data-tip"), label: el.getAttribute("data-tip-label") || "",
                hint: el.getAttribute("data-tip-hint") || "", ready: false, style: "left: 0px; top: 0px"};
            self.render();
        }, TIP_DELAY_MS);
    };

    // `anchor` is the `data-anchor` of the tile the card hangs from (a pinned copy or the tile in its lane).
    Display.prototype.openMenu = function (buttonId, anchor) {
        this.hideTip();
        this.local.prompt = null;
        this.local.menu = {buttonId: buttonId, anchor: anchor || null, ready: false, above: false, style: "left: 0px; top: 0px"};
        this.render();
    };

    Display.prototype.closeMenu = function () {
        if (this.local.menu) {
            this.local.menu = null;
            this.render();
        }
    };

    Display.prototype.openPrompt = function (buttonId, anchor) {
        this.hideTip();
        this.local.menu = null;
        this.local.prompt = {buttonId: buttonId, anchor: anchor || null, ready: false, above: false, style: "left: 0px; top: 0px"};
        this.render();
    };

    Display.prototype.closePrompt = function () {
        if (this.local.prompt) {
            this.local.prompt = null;
            this.render();
        }
    };

    Display.prototype.focusPrompt = function () {
        var field = this.container.querySelector(".deck-pop-field");
        if (field && field.focus) {
            field.focus();
            if (field.select) {
                field.select();
            }
        }
    };

    // Submit of the prompt popover: `action {id: prompt.id, value}` with the field's text, then it closes.
    Display.prototype.submitPrompt = function (promptId) {
        var field = this.container.querySelector(".deck-pop-field");
        var value = field ? String(field.value === undefined ? "" : field.value) : "";
        this.local.prompt = null;
        this.render();
        this.sendAction(promptId, value);
    };

    Display.prototype.selectTab = function (id) {
        this.local.tab = id;
        this.local.editing = false;
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

    // --- the pinned row ---

    Display.prototype.setEditing = function (on) {
        this.local.editing = !!on && !this.isOffline();
        this.hideTip();
        this.render();
    };

    /*
     * A press on a tile below the pinned row in edit mode: pins it (when there is a free place) or unpins it. The
     * row shows the change at once and sends `panel.pin` / `panel.unpin` with the action id; the host's next tree
     * is the truth. A full row shows nothing new; the host refuses the pin with a `panel.pins` result line.
     */
    Display.prototype.togglePin = function (id) {
        var ids = pinnedIds(this.tree, this.local).slice();
        var at = ids.indexOf(id);
        if (at >= 0) {
            this.unpin(id);
            return;
        }
        if (ids.length < pinsMax(this.tree)) {
            ids.push(id);
            this.local.pinsPending = ids;
            this.render();
        }
        // A full row is the host's to refuse: it answers with a `panel.pins` result line.
        this.sendAction("panel.pin", id);
    };

    Display.prototype.unpin = function (id) {
        var ids = pinnedIds(this.tree, this.local).slice();
        var at = ids.indexOf(id);
        if (at < 0) {
            return;
        }
        ids.splice(at, 1);
        this.local.pinsPending = ids;
        this.render();
        this.sendAction("panel.unpin", id);
    };

    Display.prototype.bindEvents = function () {
        var self = this;
        var box = this.container;
        box.addEventListener("click", function (event) {
            var target = event.target;
            var el;
            if (self.local.prompt) {
                if (closest(target, "data-prompt-submit", box)) {
                    self.submitPrompt(closest(target, "data-prompt-submit", box).getAttribute("data-prompt-submit"));
                    return;
                }
                var opener = closest(target, "data-prompt-open", box);
                if (!closest(target, "data-prompt-field", box) && !(opener && opener.getAttribute("data-prompt-open") === self.local.prompt.buttonId)) {
                    self.closePrompt();   // a click outside only closes the popover
                    return;
                }
                if (opener) {
                    self.closePrompt();
                }
                return;
            }
            if (self.local.menu) {
                var inMenu = closest(target, "data-menu-pick", box);
                var menuOpener = closest(target, "data-menu-open", box);
                if (!inMenu && !(menuOpener && menuOpener.getAttribute("data-menu-open") === self.local.menu.buttonId)) {
                    self.closeMenu();   // a click outside only closes the menu
                    return;
                }
            }
            if (self.local.confirming && self.local.confirmTile) {
                // An armed tile: a press on its check or cross is handled below, on the face between them nothing
                // happens, a press anywhere else only flips the tile back.
                if (!closest(target, "data-confirm-yes", box) && !closest(target, "data-confirm-no", box)) {
                    if (!closest(target, "data-armed", box)) {
                        self.setConfirm(null);
                        self.render();
                    }
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
            if ((el = closest(target, "data-pins-edit", box))) {
                self.setEditing(!self.local.editing);
                return;
            }
            if ((el = closest(target, "data-unpin", box))) {
                self.unpin(el.getAttribute("data-unpin"));
                return;
            }
            if ((el = closest(target, "data-swatch", box))) {
                // A colour cell: no pin target in edit mode; the ring moves at once, the host's next tree is the truth.
                if (!self.local.editing && !el.disabled && !self.isOffline()) {
                    var swatchId = el.getAttribute("data-swatch");
                    var picked = el.getAttribute("data-value");
                    self.local.pending[swatchId] = picked;
                    self.render();
                    self.sendAction(swatchId, picked);
                }
                return;
            }
            // Edit mode: a press on a tile below pins or unpins it; the pinned tiles only answer their cross.
            var tileEl = closest(target, "data-tile", box);
            if (self.local.editing && tileEl) {
                if (tileEl.getAttribute("data-zone") === "lane" && !self.isOffline()) {
                    self.togglePin(tileEl.getAttribute("data-tile"));
                }
                return;
            }
            if ((el = closest(target, "data-menu-open", box))) {
                if (self.local.menu) {
                    self.closeMenu();
                } else if (!self.isOffline()) {
                    self.zones[el.getAttribute("data-menu-open")] = tileEl ? tileEl.getAttribute("data-zone") : null;
                    self.openMenu(el.getAttribute("data-menu-open"), tileEl ? tileEl.getAttribute("data-anchor") : null);
                }
                return;
            }
            if ((el = closest(target, "data-prompt-open", box))) {
                if (!self.isOffline()) {
                    self.zones[el.getAttribute("data-prompt-open")] = tileEl ? tileEl.getAttribute("data-zone") : null;
                    self.openPrompt(el.getAttribute("data-prompt-open"), tileEl ? tileEl.getAttribute("data-anchor") : null);
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
                self.setConfirm(null);
                self.render();
                self.sendAction(el.getAttribute("data-action"), undefined, el.getAttribute("data-row") || undefined);
                return;
            }
            if ((el = closest(target, "data-confirm-no", box))) {
                self.setConfirm(null);
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
            if (kind !== "button" && kind !== "link" && kind !== "switch" && kind !== "auto") {
                return;
            }
            // A busy or disabled tile blocks its body; its automation capsule stays usable (PROTOCOL 6.1 `auto`).
            if (el.disabled || el.className.indexOf("is-disabled") >= 0 || el.className.indexOf("is-busy") >= 0 ||
                    (kind !== "auto" && tileEl && tileEl.className.indexOf("is-busy") >= 0)) {
                return;
            }
            var id = el.getAttribute("data-action");
            if (tileEl) {
                self.zones[tileEl.getAttribute("data-tile")] = tileEl.getAttribute("data-zone");
            }
            if (kind === "switch" || kind === "auto") {
                self.flipSwitch(id, el.getAttribute("data-value") !== "true");
                return;
            }
            if (el.hasAttribute("data-confirm")) {
                var key = el.getAttribute("data-confirm");
                var arming = self.local.confirming !== key;
                self.setConfirm(arming ? key : null, !!tileEl);
                self.render();
                if (arming && tileEl && tileEl.getAttribute("data-tip-armed")) {
                    // The question is the tile's tooltip, shown at once.
                    self.local.tip = {key: tileEl.getAttribute("data-tip"), label: tileEl.getAttribute("data-tip-label") || "",
                        hint: "", ready: false, style: "left: 0px; top: 0px"};
                    self.render();
                }
                return;
            }
            self.sendAction(id, undefined, el.getAttribute("data-row") || undefined);
        });
        box.addEventListener("contextmenu", function (event) {
            var tileEl = closest(event.target, "data-tile", box);
            var corner = tileEl ? tileEl.querySelector(".deck-corner") : null;
            if (corner && !self.isOffline() && !self.local.editing) {
                event.preventDefault();
                self.zones[tileEl.getAttribute("data-tile")] = tileEl.getAttribute("data-zone");
                if (corner.hasAttribute("data-menu-open")) {
                    self.openMenu(corner.getAttribute("data-menu-open"), tileEl.getAttribute("data-anchor"));
                } else {
                    self.openPrompt(corner.getAttribute("data-prompt-open"), tileEl.getAttribute("data-anchor"));
                }
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
        box.addEventListener("input", function (event) {
            var el = event.target;
            if (el.getAttribute && el.hasAttribute("data-scale")) {
                self.setScale(el.value);
            }
        });
        box.addEventListener("keydown", function (event) {
            var el = event.target;
            if (event.key === "Enter" && el.getAttribute && el.hasAttribute("data-prompt-field")) {
                event.preventDefault();
                self.submitPrompt(el.getAttribute("data-prompt-field"));
            } else if (event.key === "Enter" && el.getAttribute && el.getAttribute("data-kind") === "input") {
                event.preventDefault();
                self.sendAction(el.getAttribute("data-action"), el.value);
            } else if (event.key === "Escape") {
                if (self.local.prompt) {
                    self.closePrompt();
                } else if (self.local.menu) {
                    self.closeMenu();
                } else if (self.local.confirming) {
                    self.setConfirm(null);
                    self.render();
                } else if (self.local.tip) {
                    self.hideTip();
                } else if (self.closeDetails()) {
                    self.render();
                } else if (self.local.editing) {
                    self.setEditing(false);
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
            if (self.armedTip()) {
                return;   // the question of an armed tile stays until the tile flips back
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
            if (self.armedTip()) {
                return;
            }
            self.hideTip();
        });
        box.addEventListener("mousedown", function () {
            if (!self.armedTip()) {
                self.hideTip();
            }
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
            self.closePrompt();
        }, true);
        this.win.addEventListener("resize", function () {
            self.hideTip();
            self.closeMenu();
            self.closePrompt();
            self.local.viewHeight = Number(self.win.innerHeight) || STAGE_H;
            self.render();
            if (self.resizeTimer) {
                clearTimeout(self.resizeTimer);
            }
            self.resizeTimer = setTimeout(function () {
                self.resizeTimer = null;
                self.sendReady();
            }, RESIZE_DEBOUNCE_MS);
        });
    };

    // A switch or capsule click (PROTOCOL 6.1): the tile flips at once and the action asks for the new value; the
    // host's next tree replaces the optimistic value (a refused change comes back as the old one).
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
        stageRect: stageRect,
        planThemeSwitch: planThemeSwitch,
        normaliseTheme: normaliseTheme,
        findButton: findButton,
        lineKeyFor: lineKeyFor,
        lineKeys: lineKeys,
        groupId: groupId,
        laneCode: laneCode,
        pinsSectionId: pinsSectionId,
        selectedTab: selectedTab,
        footerVersion: footerVersion,
        clampScale: clampScale,
        initialScale: initialScale,
        stageStyle: stageStyle,
        clock: clock,
        Display: Display,
        LATE_MS: LATE_MS,
        RESULT_MS: RESULT_MS,
        LINE_MS: LINE_MS,
        LINE_ERROR_MS: LINE_ERROR_MS,
        TIP_DELAY_MS: TIP_DELAY_MS,
        CONFIRM_MS: CONFIRM_MS,
        STAGE_W: STAGE_W,
        STAGE_H: STAGE_H,
        PINS_MAX: PINS_MAX,
        KEY_SCALE: KEY_SCALE
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
