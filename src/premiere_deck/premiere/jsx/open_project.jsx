// open_project: app.openDocument with the conversion, locate-file and warning dialogs suppressed. __args = {path}.
var file = new File(__args.path);
if (!file.exists) {
    throw new Error("project file not found: " + __args.path);
}
var ok = app.openDocument(file.fsName, true, true, true, false);
return {ok: !!ok, path: app.project ? String(app.project.path) : null};
