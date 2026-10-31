// The path of one of Premiere's audio-only system presets: <Premiere folder>\MediaIO\systempresets\<folder>\<file>.
// The Premiere folder comes from app.path (the folder itself, or the folder of the program file).
// args: {folder, file, label}
// returns: {path}
var roots = [];
try {
    var base = String(app.path || "");
    if (base) {
        roots.push(base);
        var asFile = new File(base);
        if (asFile.parent) {
            roots.push(asFile.parent.fsName);
        }
    }
} catch (ePath) {}
for (var i = 0; i < roots.length; i++) {
    var root = roots[i].replace(/[\\\/]+$/, "");
    var preset = new File(root + "\\MediaIO\\systempresets\\" + __args.folder + "\\" + __args.file);
    if (preset.exists) {
        return {path: preset.fsName};
    }
}
throw new Error("deck: The " + __args.label + " preset of Premiere was not found.");
