// @uses lib
// relink: changeMediaPath and re-read the offline state (docs/PREMIERE_API.md 7.1). __args = {node_id, media_path}.
var item = requireItem(__args.node_id);
if (!item.canChangeMediaPath()) {
    throw new Error("Premiere cannot change the media path of " + item.name);
}
var raw = item.changeMediaPath(__args.media_path, true);
return {raw: raw === undefined ? null : raw, online: !item.isOffline(), media_path: safeMediaPath(item)};
