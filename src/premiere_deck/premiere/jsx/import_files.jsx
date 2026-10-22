// @uses lib
// import_files: imports files into the bin at tree_path (created when missing) and returns the items created (the
// bin's children before and after). Images are imported one at a time (docs/PREMIERE_API.md 7.2).
// __args = {paths, tree_path, suppress_dialogs}.
var IMAGE = /\.(png|jpe?g|tiff?|bmp|gif|psd|tga|exr|dpx|webp|heic)$/i;
var bin = binByPath(__args.tree_path, true);
var suppress = __args.suppress_dialogs !== false;
var before = {};
for (var i = 0; i < bin.children.numItems; i++) {
    before[String(bin.children[i].nodeId)] = true;
}
var existing = [];
var missing = [];
var images = [];
for (var p = 0; p < __args.paths.length; p++) {
    var path = String(__args.paths[p]);
    if (!new File(path).exists) {
        missing.push(path);
    } else if (IMAGE.test(path)) {
        images.push(path);
    } else {
        existing.push(path);
    }
}
var ok = true;
if (existing.length) {
    ok = app.project.importFiles(existing, suppress, bin, false) && ok;
}
for (var m = 0; m < images.length; m++) {
    ok = app.project.importFiles([images[m]], suppress, bin, false) && ok;
}
var items = [];
var imported = {};
var treePath = splitTreePath(__args.tree_path).join("/");
for (var j = 0; j < bin.children.numItems; j++) {
    var child = bin.children[j];
    if (!before[String(child.nodeId)]) {
        var info = itemInfo(child, treePath);
        items.push(info);
        if (info.media_path) {
            imported[info.media_path.toLowerCase()] = true;
        }
    }
}
// Premiere can return true and still create nothing for a file (docs/PREMIERE_API.md 7.2).
var failed = [];
var requested = existing.concat(images);
for (var f = 0; f < requested.length; f++) {
    if (!imported[new File(requested[f]).fsName.toLowerCase()]) {
        failed.push(requested[f]);
    }
}
return {ok: !!ok, items: items, missing: missing, failed: failed};
