// workspace: lists workspaces, or switches when __args.set is a name. __args = {set}.
var names = [];
var list = app.getWorkspaces();
for (var i = 0; i < list.length; i++) {
    names.push(String(list[i]));
}
if (__args.set === null || __args.set === undefined) {
    return {workspaces: names};
}
for (var j = 0; j < names.length; j++) {
    if (names[j] === __args.set) {
        app.setWorkspace(names[j]);
        return {ok: true, workspaces: names};
    }
}
return {ok: false, workspaces: names};
