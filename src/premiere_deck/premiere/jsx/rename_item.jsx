// @uses lib
// rename_item: __args = {node_id, name}.
var item = requireItem(__args.node_id);
item.name = String(__args.name);
return {name: String(item.name)};
