// @uses lib
// remove_items: removes project items by node id. ExtendScript has no delete for clips, so the items are moved into
// a fresh holding bin and that bin is deleted. Files on disk are not touched. __args = {node_ids}.
var ids = (__args && __args.node_ids) || [];
var root = app.project.rootItem;
var holderName = "Premiere Deck removal " + (new Date()).getTime();
var holder = root.createBin(holderName);
if (!holder) {
    for (var c = 0; c < root.children.numItems; c++) {
        if (root.children[c].name === holderName) { holder = root.children[c]; break; }
    }
}
if (!holder) { throw new Error("Could not create the holding bin"); }
var removed = 0;
var missing = [];
for (var i = 0; i < ids.length; i++) {
    var item = findByNodeId(root, String(ids[i]));
    if (!item) { missing.push(String(ids[i])); continue; }
    item.moveBin(holder);
    removed++;
}
holder.deleteBin();
return {removed: removed, missing: missing};
