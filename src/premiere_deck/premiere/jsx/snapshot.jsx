// @uses lib
// snapshot: the project tree in pages (docs/PREMIERE_API.md 2, PROTOCOL 6.4). __args = {start, limit, token}.
// The first page (token null) walks the tree once and keeps the item references in $.global.__deckSnapshot; the
// following pages describe the next `limit` items. A page with another token, or after the project changed,
// answers {stale: true} and the facade starts over.
var project = app.project;
if (!project || !project.path) {
    return null;
}
var cache = $.global.__deckSnapshot;
if (__args.token) {
    if (!cache || cache.token !== __args.token || cache.path !== String(project.path)) {
        return {stale: true};
    }
} else {
    var entries = [];
    walk(project.rootItem, function (item, treePath) {
        entries.push([item, treePath]);
    });
    cache = {
        token: String(new Date().getTime()) + "-" + Math.floor(Math.random() * 1000000),
        path: String(project.path),
        entries: entries
    };
    $.global.__deckSnapshot = cache;
}
var start = Number(__args.start) || 0;
var limit = Number(__args.limit) || 2000;
var end = Math.min(cache.entries.length, start + limit);
var items = [];
for (var i = start; i < end; i++) {
    try {
        items.push(itemInfo(cache.entries[i][0], cache.entries[i][1]));
    } catch (e) {
        // The item was removed between pages; the next snapshot sees the current state.
    }
}
var next = end < cache.entries.length ? end : null;
if (next === null) {
    $.global.__deckSnapshot = null;
}
return {token: cache.token, project_path: cache.path, total: cache.entries.length, items: items, next: next};
