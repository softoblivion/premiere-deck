// save: app.project.save(), or saveAs(path) when __args.path is set; saveAs makes path the open project.
if (!app.project || !app.project.path) {
    throw new Error("no project is open");
}
if (__args.path) {
    app.project.saveAs(String(__args.path));
} else {
    app.project.save();
}
return {path: String(app.project.path)};
