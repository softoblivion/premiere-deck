/*
 * Premiere Deck panel: the headless agent (docs/PANEL.md 1, docs/PROTOCOL.md 4, 5, 7, 8).
 * Runs the scripts the host sends (one at a time, with a timeout), forwards Premiere events, polls the project
 * every 2 s, sends a heartbeat every 10 s and starts the host exe when allowed. No UI.
 * The pure helpers (wrapScript, parseResult, pageResult, ...) are exported for `node tests/panel/run.js`.
 */
(function (root) {
    "use strict";

    var EVENT_TYPE = "com.softoblivion.premieredeck.event";
    var POLL_MS = 2000;
    var HEARTBEAT_MS = 10000;
    var AUTOSTART_AFTER_MS = 5000;
    var EVENT_COALESCE_MS = 100;
    var DEFAULT_TIMEOUT_MS = 30000;
    // Results above this many UTF-8 bytes are paged (PROTOCOL 6.4); each chunk stays at or below it.
    var PAGE_BYTES = 6 * 1024 * 1024;
    // Lines of the wrapper before the script's first line (keeps error line numbers relative to the script).
    var WRAPPER_LINES = 3;

    // Protocol event -> Premiere bindings (verified on Premiere 26.5.2: docs/PREMIERE_API.md 6). Premiere 26.5 fires
    // onItemsAddedToProjectSuccess (plural) and, for timeline edits, onActiveSequenceChanged and
    // onActiveSequenceTrackItemAdded / Removed; the singular and the StructureChanged names are bound for older
    // versions that may fire them.
    var EVENT_BINDINGS = {
        "project.items_added": ["onItemsAddedToProjectSuccess", "onItemAddedToProjectSuccess"],
        "project.changed": ["onProjectChanged"],
        "sequence.activated": ["onSequenceActivated"],
        "sequence.structure_changed": ["onActiveSequenceChanged", "onActiveSequenceTrackItemAdded",
            "onActiveSequenceTrackItemRemoved", "onActiveSequenceStructureChanged"],
        "sequence.selection_changed": ["onActiveSequenceSelectionChanged"]
    };
    // Events the agent always sends; the facade keeps the project state from them (PREMIERE_API 4).
    var ALWAYS_SENT = {"project.opened": true, "project.closed": true};

    // --- pure helpers ------------------------------------------------------------------------------------------

    // A JSON text that is also a valid ExtendScript literal (U+2028 / U+2029 end lines in ES3).
    function jsLiteral(value) {
        var text = JSON.stringify(value === undefined ? null : value);
        var backslash = String.fromCharCode(92);
        return text.split(String.fromCharCode(0x2028)).join(backslash + "u2028")
            .split(String.fromCharCode(0x2029)).join(backslash + "u2029");
    }

    // The program evalScript runs for one host script (PROTOCOL 7). The script is the body of a nested function
    // and the try/catch sits around its call: ExtendScript (ES3) rejects function declarations inside a try block,
    // and lib.jsx and many scripts declare functions.
    function wrapScript(code, args) {
        return "(function(){\n" +
            "var __args = " + jsLiteral(args) + ";\n" +
            "function __deckBody() {\n" +
            code + "\n" +
            "}\n" +
            "var __r;\n" +
            "try { __r = __deckBody(); } catch (__e) { __r = __deckError(__e, " +
            jsLiteral(String(code).substring(0, 200)) + ", " + WRAPPER_LINES + "); }\n" +
            "return __deckEncode(__r);\n" +
            "})()";
    }

    // Decodes evalScript's raw string into {ok, result} or {ok: false, error: {message, line, source}}.
    function parseResult(raw, code) {
        var source = String(code || "").substring(0, 200);
        if (raw === undefined || raw === null || raw === "" || raw === "undefined") {
            return {ok: false, error: {message: "empty result from evalScript", line: null, source: source}, syntax: false};
        }
        var text = String(raw);
        if (/^EvalScript error/i.test(text)) {
            return {ok: false, error: {message: "EvalScript error", line: null, source: source}, syntax: true};
        }
        var parsed;
        try {
            parsed = JSON.parse(text);
        } catch (e) {
            return {ok: false, error: {message: "unexpected result: " + text.substring(0, 200), line: null, source: source}, syntax: false};
        }
        if (!parsed || typeof parsed !== "object" || typeof parsed.ok !== "boolean") {
            return {ok: false, error: {message: "unexpected result: " + text.substring(0, 200), line: null, source: source}, syntax: false};
        }
        if (parsed.ok) {
            return {ok: true, result: parsed.result === undefined ? null : parsed.result};
        }
        var error = parsed.error || {};
        return {
            ok: false,
            error: {message: String(error.message || "error"), line: error.line === undefined ? null : error.line, source: String(error.source || source)},
            syntax: false
        };
    }

    function utf8Length(text) {
        var bytes = 0;
        for (var i = 0; i < text.length; i++) {
            var c = text.charCodeAt(i);
            if (c < 0x80) {
                bytes += 1;
            } else if (c < 0x800) {
                bytes += 2;
            } else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
                bytes += 4;
                i++;
            } else {
                bytes += 3;
            }
        }
        return bytes;
    }

    // Splits `text` into pieces of at most `maxBytes` UTF-8 bytes, never inside a surrogate pair.
    function splitUtf8(text, maxBytes) {
        var chunks = [];
        var start = 0;
        var bytes = 0;
        for (var i = 0; i < text.length; i++) {
            var c = text.charCodeAt(i);
            var size = c < 0x80 ? 1 : c < 0x800 ? 2 : (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) ? 4 : 3;
            if (bytes + size > maxBytes && i > start) {
                chunks.push(text.substring(start, i));
                start = i;
                bytes = 0;
            }
            bytes += size;
            if (size === 4) {
                i++;
            }
        }
        if (start < text.length || chunks.length === 0) {
            chunks.push(text.substring(start));
        }
        return chunks;
    }

    // The script.result message(s) for one outcome: one message, or pages when the result is large (6.4).
    function resultMessages(id, outcome, ms, pageBytes) {
        var limit = pageBytes || PAGE_BYTES;
        if (!outcome.ok) {
            return [{type: "script.result", id: id, ok: false, error: outcome.error, ms: ms}];
        }
        var single = {type: "script.result", id: id, ok: true, result: outcome.result, ms: ms};
        var text = JSON.stringify(outcome.result);
        if (utf8Length(text) + 200 <= limit) {
            return [single];
        }
        var chunks = splitUtf8(text, limit);
        var messages = [];
        for (var i = 0; i < chunks.length; i++) {
            messages.push({type: "script.result", id: id, ok: true, page: i + 1, pages: chunks.length, chunk: chunks[i], ms: ms});
        }
        return messages;
    }

    // Bindings to register for a list of protocol events.
    function bindingsFor(events) {
        var seen = {};
        var names = [];
        for (var i = 0; i < events.length; i++) {
            var list = EVENT_BINDINGS[events[i]] || [];
            for (var j = 0; j < list.length; j++) {
                if (!seen[list[j]]) {
                    seen[list[j]] = true;
                    names.push(list[j]);
                }
            }
        }
        return names;
    }

    // Protocol event of a Premiere binding.
    function eventForBinding(binding) {
        for (var name in EVENT_BINDINGS) {
            if (EVENT_BINDINGS.hasOwnProperty(name) && EVENT_BINDINGS[name].indexOf(binding) >= 0) {
                return name;
            }
        }
        return null;
    }

    // Events implied by two consecutive polls ({path, count}); `prev` null on the first poll.
    function pollEvents(prev, next) {
        var events = [];
        var before = prev ? prev.path : null;
        if (before !== next.path) {
            if (before) {
                events.push({name: "project.closed", data: {path: before}});
            }
            if (next.path) {
                events.push({name: "project.opened", data: {path: next.path}});
            }
        } else if (next.path && prev && prev.count !== null && next.count !== null && next.count > prev.count) {
            events.push({name: "project.items_added", data: {}});
        }
        return events;
    }

    // --- runtime (CEP only) ------------------------------------------------------------------------------------

    function Agent() {
        this.common = root.DeckCommon;
        this.cs = this.common.csInterface();
        this.client = null;
        this.queue = [];
        this.busy = null;          // {kind: "script"|"internal", ...}
        this.ready = false;
        this.subscribed = {};
        this.boundBindings = {};
        this.state = null;         // last poll {path, count, sequence_id}
        this.pending = {};         // coalesced binding events by protocol name
        this.recent = [];
        this.startedAt = Date.now();
    }

    Agent.prototype.start = function () {
        var self = this;
        this.client = new this.common.DeckClient({
            role: "agent",
            projectPath: function () {
                return self.state ? self.state.path : null;
            },
            onMessage: function (message) {
                self.onMessage(message);
            },
            onState: function (state) {
                if (state === "connected") {
                    self.sendHeartbeat();
                }
            }
        });
        this.cs.addEventListener(EVENT_TYPE, function (event) {
            self.onBindingEvent(event);
        });
        this.ensureHelpers(function () {
            self.poll(function () {
                self.client.start();
            });
        });
        setInterval(function () {
            self.poll();
        }, POLL_MS);
        setInterval(function () {
            self.sendHeartbeat();
        }, HEARTBEAT_MS);
        setTimeout(function () {
            self.maybeStartHost();
        }, AUTOSTART_AFTER_MS);
    };

    Agent.prototype.log = function (level, message) {
        if (this.client) {
            this.client.log(level, message);
        }
    };

    // host.jsx is loaded by the manifest's ScriptPath only when Premiere starts the extension; a page reload after a
    // file update would keep the old helpers in Premiere's engine. So the agent evaluates the file at every start.
    Agent.prototype.ensureHelpers = function (done) {
        var self = this;
        var path = this.common.joinPath(this.common.extensionDir(), "host.jsx").replace(/\\/g, "/");
        var program = "try { $.evalFile(" + JSON.stringify(path) + "); } catch (e) {} " +
            "typeof __deckReady === 'function' ? __deckReady() : 'missing'";
        this.cs.evalScript(program, function (raw) {
            if (raw !== "ready") {
                // Scripts still run and fail with a readable error instead of waiting forever.
                self.log("error", "host.jsx helpers unavailable: " + raw);
            }
            self.ready = true;
            done();
        });
    };

    Agent.prototype.onMessage = function (message) {
        if (message.type === "script") {
            this.queue.push({kind: "script", message: message});
            this.pump();
        } else if (message.type === "subscribe") {
            this.subscribe(message.events || []);
        }
        // The host never opens the visible panel (PROTOCOL 3); a 1.x `open_display` is ignored like any unknown type.
    };

    Agent.prototype.subscribe = function (events) {
        var self = this;
        this.subscribed = {};
        for (var i = 0; i < events.length; i++) {
            this.subscribed[events[i]] = true;
        }
        var missing = [];
        var names = bindingsFor(events);
        for (var j = 0; j < names.length; j++) {
            if (!this.boundBindings[names[j]]) {
                missing.push(names[j]);
            }
        }
        if (!missing.length) {
            return;
        }
        this.queue.push({
            kind: "internal",
            code: "__deckBind(" + JSON.stringify(JSON.stringify(missing)) + ")",
            done: function (raw) {
                var result = {};
                try {
                    result = JSON.parse(raw);
                } catch (e) {
                    self.log("error", "app.bind failed: " + raw);
                    return;
                }
                for (var name in result) {
                    if (result.hasOwnProperty(name) && result[name] === true) {
                        self.boundBindings[name] = true;
                    }
                }
                self.log("info", "bound " + JSON.stringify(result));
            }
        });
        this.pump();
    };

    // Runs the next queued job when nothing is in flight. Host scripts and internal calls share one slot.
    Agent.prototype.pump = function () {
        var self = this;
        if (this.busy || !this.queue.length || !this.ready) {
            return;
        }
        var job = this.queue.shift();
        this.busy = job;
        if (job.kind === "internal") {
            this.cs.evalScript(job.code, function (raw) {
                self.busy = null;
                try {
                    job.done(raw);
                } finally {
                    self.pump();
                }
            });
            return;
        }
        var message = job.message;
        var code = String(message.code || "");
        var startedAt = Date.now();
        var timeoutMs = Number(message.timeout_ms) > 0 ? Number(message.timeout_ms) : DEFAULT_TIMEOUT_MS;
        job.timedOut = false;
        job.timer = setTimeout(function () {
            job.timedOut = true;
            self.sendResult(message.id, {ok: false, error: {message: "timeout", line: null, source: code.substring(0, 200)}},
                Date.now() - startedAt);
            self.log("warning", "script " + message.name + " exceeded " + timeoutMs + " ms; the next script waits for it");
        }, timeoutMs);
        this.cs.evalScript(wrapScript(code, message.args), function (raw) {
            clearTimeout(job.timer);
            var ms = Date.now() - startedAt;
            var outcome = parseResult(raw, code);
            var finish = function (result) {
                self.busy = null;
                if (job.timedOut) {
                    self.log("warning", "script " + message.name + " finished after its timeout (" + ms + " ms, ok=" + result.ok + ")");
                } else {
                    self.sendResult(message.id, result, ms);
                }
                self.pump();
            };
            if (outcome.syntax) {
                self.syntaxDetail(wrapScript(code, message.args), code, outcome, finish);
            } else {
                finish(outcome);
            }
        });
    };

    // After "EvalScript error.": compile the program once more to learn the syntax error and its line.
    Agent.prototype.syntaxDetail = function (program, code, outcome, finish) {
        this.cs.evalScript("__deckCheckSyntax(" + jsLiteral(program) + ")", function (raw) {
            var detail = null;
            try {
                detail = raw ? JSON.parse(raw) : null;
            } catch (e) {
                detail = null;
            }
            if (detail && detail.message) {
                // new Function() adds one line of its own before the body.
                var line = detail.line === null ? null : detail.line - WRAPPER_LINES - 1;
                outcome = {ok: false, error: {message: "SyntaxError: " + detail.message, line: line, source: code.substring(0, 200)}};
            }
            finish(outcome);
        });
    };

    Agent.prototype.sendResult = function (id, outcome, ms) {
        var messages = resultMessages(id, outcome, ms);
        for (var i = 0; i < messages.length; i++) {
            if (!this.client.send(messages[i])) {
                this.log("warning", "result of " + id + " dropped: not connected");
                return;
            }
        }
    };

    Agent.prototype.poll = function (done) {
        var self = this;
        if (this.busy || this.queue.length || !this.ready) {
            if (done) {
                done();
            }
            return;
        }
        this.busy = {kind: "poll"};
        this.cs.evalScript("__deckState()", function (raw) {
            self.busy = null;
            var next = null;
            try {
                next = JSON.parse(raw);
            } catch (e) {
                next = null;
            }
            if (next && typeof next === "object" && !next.error) {
                var events = pollEvents(self.state, next);
                self.state = next;
                for (var i = 0; i < events.length; i++) {
                    self.emit(events[i].name, events[i].data);
                }
            }
            if (done) {
                done();
            }
            self.pump();
        });
    };

    Agent.prototype.emit = function (name, data) {
        if (!ALWAYS_SENT[name] && !this.subscribed[name]) {
            return;
        }
        if (this.client) {
            this.client.send({type: "event", name: name, data: data || {}, at: Date.now()});
        }
    };

    Agent.prototype.onBindingEvent = function (event) {
        var self = this;
        var payload = event.data;
        if (typeof payload === "string") {
            try {
                payload = JSON.parse(payload);
            } catch (e) {
                return;
            }
        }
        if (!payload || !payload.binding) {
            return;
        }
        // Last raw bindings, for diagnostics over DevTools (deckAgent.recent).
        this.recent.push({binding: payload.binding, at: Date.now()});
        if (this.recent.length > 50) {
            this.recent.shift();
        }
        var name = eventForBinding(payload.binding);
        if (!name) {
            return;
        }
        var data = payload.data || {};
        if (name === "project.items_added" && this.state) {
            // The poll would report the same items again; count them as seen.
            this.state.count = null;
        }
        var first = !this.pending[name];
        this.pending[name] = data;
        if (first) {
            setTimeout(function () {
                var latest = self.pending[name];
                delete self.pending[name];
                self.emit(name, latest);
            }, EVENT_COALESCE_MS);
        }
    };

    Agent.prototype.sendHeartbeat = function () {
        if (!this.client) {
            return;
        }
        this.client.send({
            type: "heartbeat",
            project_path: this.state ? this.state.path : null,
            active_sequence_id: this.state ? this.state.sequence_id : null,
            at: Date.now()
        });
    };

    // PROTOCOL 2: no connection within 5 s, autostart allowed, exe present -> start it once per Premiere session.
    Agent.prototype.maybeStartHost = function () {
        if (this.client.handshaken || this.client.connectedOnce) {
            return;
        }
        var deck = this.common.readDeck();
        if (!deck || !deck.autostart_host || !deck.host_exe) {
            return;
        }
        var key = "deck.autostarted";
        try {
            if (root.sessionStorage.getItem(key)) {
                return;
            }
            root.sessionStorage.setItem(key, String(Date.now()));
        } catch (e) {
            // No storage: start anyway (once per page life).
        }
        var status = this.common.startHost(deck, ["--from-premiere"]);
        this.log("info", "host autostart: " + status);
    };

    var api = {
        EVENT_BINDINGS: EVENT_BINDINGS,
        WRAPPER_LINES: WRAPPER_LINES,
        PAGE_BYTES: PAGE_BYTES,
        jsLiteral: jsLiteral,
        wrapScript: wrapScript,
        parseResult: parseResult,
        utf8Length: utf8Length,
        splitUtf8: splitUtf8,
        resultMessages: resultMessages,
        bindingsFor: bindingsFor,
        eventForBinding: eventForBinding,
        pollEvents: pollEvents,
        Agent: Agent
    };

    root.DeckAgent = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    } else if (root.document) {
        root.deckAgent = new Agent();
        root.deckAgent.start();
    }
}(typeof window !== "undefined" ? window : this));
