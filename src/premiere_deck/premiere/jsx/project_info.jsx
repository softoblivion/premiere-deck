// project_info: path and name of the open project, or null (docs/PREMIERE_API.md 2). The root folder is derived
// by the facade.
var project = app.project;
if (!project || !project.path) {
    return null;
}
return {path: String(project.path), name: String(project.name)};
