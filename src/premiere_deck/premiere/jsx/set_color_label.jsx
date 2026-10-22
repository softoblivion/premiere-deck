// @uses lib
// set_color_label: label index 0..15. __args = {node_ids, label}.
var missing = [];
for (var i = 0; i < __args.node_ids.length; i++) {
    var item = findByNodeId(app.project.rootItem, __args.node_ids[i]);
    if (!item) {
        missing.push(String(__args.node_ids[i]));
        continue;
    }
    item.setColorLabel(Number(__args.label));
}
return {missing: missing};
