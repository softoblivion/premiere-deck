// @uses lib
// move_to_bin: moves items into the bin at tree_path (created when missing). __args = {node_ids, tree_path}.
var bin = binByPath(__args.tree_path, true);
var moved = 0;
var missing = [];
for (var i = 0; i < __args.node_ids.length; i++) {
    var item = findByNodeId(app.project.rootItem, __args.node_ids[i]);
    if (!item) {
        missing.push(String(__args.node_ids[i]));
        continue;
    }
    item.moveBin(bin);
    moved++;
}
return {moved: moved, missing: missing, bin_id: String(bin.nodeId)};
