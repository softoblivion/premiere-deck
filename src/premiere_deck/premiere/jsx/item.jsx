// @uses lib
// item: one project item by node id, or null. __args = {node_id}.
var item = findByNodeId(app.project.rootItem, __args.node_id);
return item ? itemInfo(item) : null;
