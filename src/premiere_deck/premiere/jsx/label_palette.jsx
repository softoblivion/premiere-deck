// label_palette: Premiere's 16 label names and colours as its preferences hold them (Preferences > Labels), read
// through app.properties (keys BE.Prefs.LabelNames.<n> and BE.Prefs.LabelColors.<n>, the colour a Windows COLORREF
// number). A key that is missing or unreadable gives null; the facade puts the default in its place
// (docs/PREMIERE_API.md 7.19). __args: none.
// returns: {available (app.properties exists), labels: [{index, name, colour}]}
var props = null;
try {
    props = app.properties;
} catch (eProps) {
    props = null;
}

var read = function (key) {
    if (!props) {
        return null;
    }
    try {
        if (typeof props.doesPropertyExist === "function" && !props.doesPropertyExist(key)) {
            return null;
        }
        var value = props.getProperty(key);
        return value === undefined || value === null ? null : String(value);
    } catch (eRead) {
        return null;
    }
};

var labels = [];
for (var i = 0; i < 16; i++) {
    labels.push({index: i, name: read("BE.Prefs.LabelNames." + i), colour: read("BE.Prefs.LabelColors." + i)});
}
return {available: !!props, labels: labels};
