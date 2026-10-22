// @uses lib
// create_bin_path: creates missing bins along the path; returns the leaf bin's node id. __args = {tree_path}.
var bin = binByPath(__args.tree_path, true);
return {node_id: String(bin.nodeId)};
